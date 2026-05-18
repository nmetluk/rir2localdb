# Followup: `rir2localdb-serve.service` systemd unit

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Инициатор:** реальный feedback от первого пользователя
(whois-watcher integration). Stage 3-01 покрывал только daily oneshot
sync через timer; long-running API оставался без autostart на
bare-metal. Лишний выбор «tmux без autostart» vs «Docker compose» —
закрыли третьим юнитом.
**Коммиты:** `feat(deploy): rir2localdb-serve.service для long-running
API`, `fix(cli): log_config=None в serve`, `feat(deploy):
install-systemd.sh enables serve.service`, `ci: lint serve.service`,
`docs: HTTP API server via systemd`, `docs(changelog): unreleased`,
`docs(session-log): 03-followup serve.service`.

## Что сделано

### `deploy/systemd/rir2localdb-serve.service`

- `Type=simple`, `Restart=on-failure`, `RestartSec=10s`,
  `StartLimitBurst=5` за `StartLimitIntervalSec=300` (в `[Unit]` —
  modern placement; в `[Service]` deprecated в новых systemd).
- **Параметризуемый bind** через
  `EnvironmentFile=-/etc/rir2localdb/serve.env`. Leading dash —
  optional file. Defaults: `RIR2LOCALDB_SERVE_HOST=127.0.0.1`,
  `PORT=8000`. ExecStart использует `${RIR2LOCALDB_SERVE_HOST}` /
  `${RIR2LOCALDB_SERVE_PORT}`.
- **Hardening**: NoNewPrivileges, PrivateTmp, ProtectSystem=strict,
  ProtectHome=read-only (без ReadWritePaths — serve не пишет файлы),
  ProtectKernel*, RestrictNamespaces, MemoryDenyWriteExecute,
  LockPersonality, RestrictSUIDSGID, SystemCallArchitectures=native.
- `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX **AF_NETLINK**` —
  netlink нужен для async DNS resolution в asyncpg/httpx (libc
  resolver hints через netlink-socket); без AF_NETLINK — загадочные
  timeouts. У sync.service netlink не нужен (он использует HTTP, для
  которого httpx собственный resolver).
- `MemoryMax=1G` (live smoke: peak 73MB).
- `ConditionPathExists=/home/rir2local/rir2localdb/.venv/bin/rir2localdb`
  — sanity check.

### CLI fix: `uvicorn.run(..., log_config=None)`

Без этого uvicorn ставит свой dictConfig на root logger при
старте — наш structlog setup из `configure_logging()` перебивается,
JSON-логи в production-режиме теряются. Fix verified live:

```
journalctl -u rir2localdb-serve -o cat | head -3
Started rir2localdb-serve.service ...
{"event": "Started server process [1670267]", "logger": "uvicorn.error", "level": "info", ...}
{"event": "Waiting for application startup.", ...}
```

Single-line JSON для uvicorn.error / uvicorn.access после fix'а.

### `scripts/install-systemd.sh`

Loop по трём юнитам (sync.service, sync.timer, serve.service).
`enable --now sync.timer`, **только enable** для serve.service
(оператор сам решает когда стартовать).

`install -d -m 0755 /etc/rir2localdb` — общий каталог для override
файлов (serve.env, telegram_bot_token, и т.п.).

Финальный output показывает примеры override и `systemctl start`.

### CI

Шаг `lint systemd units` loop'ит sed-обработкой по обоим service'ам.
Дополнительный sed-pattern `-e '/^EnvironmentFile=/d'` убирает
строку, потому что на CI runner'е `/etc/rir2localdb/serve.env` не
существует.

### Документация

- `docs/07-operations.md` § «HTTP API server via systemd»: setup,
  override через serve.env, public vs private bridge bind rationale,
  smoke commands.
- `README.md` Quick start: + одна строка про `systemctl start
  rir2localdb-serve.service`.
- `CHANGELOG.md`: открыт раздел `[Unreleased]` поверх замороженного
  `[0.1.0]`.

## Почему дефолт 127.0.0.1, а не 0.0.0.0

Safe default. На свежей машине после `install-systemd.sh + start`
сервис вылезает на loopback only. Чтобы expose'нуться публично —
явный override `RIR2LOCALDB_SERVE_HOST=0.0.0.0` через `serve.env`.
Это лучше «по умолчанию открыто, забудешь firewall — пробив»: даже
при misconfig firewall'а ничего не утекает.

Также pattern «private bridge IP» (например `172.28.0.1` для Docker
compose gateway) — более защищён чем `0.0.0.0` + firewall rule.

## Зачем `AF_NETLINK`

asyncpg/httpx используют асинхронный DNS resolver. Glibc'овский
NSS modul'ь иногда делает netlink-socket вызов чтобы прочитать
routing hints (выбрать source-IP для outgoing connection). Без
`AF_NETLINK` в whitelist'е → resolver падает в timeout с
`EAFNOSUPPORT`, не в ошибку, что особенно ловко прятать.

Для sync.service это не выявилось потому что он использовал AF_INET
только для исходящих HTTPS (httpx сам делает resolver, не дёргает
libc NSS). Но serve может позже вызвать `socket.getaddrinfo` через
другой кодпуть (например, при попытке самокоммуникации).
Добавил `AF_NETLINK` сразу — дёшево и страхует.

## Live smoke ✅

```
$ sudo bash scripts/install-systemd.sh
installed: /etc/systemd/system/rir2localdb-sync.service
installed: /etc/systemd/system/rir2localdb-sync.timer
installed: /etc/systemd/system/rir2localdb-serve.service
... systemd-analyze verify: clean ...
Created symlink ... → rir2localdb-serve.service

$ sudo systemctl start rir2localdb-serve.service
$ curl -fsS http://127.0.0.1:8000/v1/healthz
{"status":"ok"}
$ curl -fsS http://127.0.0.1:8000/v1/readyz
{"status":"ready"}

$ sudo systemctl status rir2localdb-serve.service
● rir2localdb-serve.service - rir2localdb HTTP API server (FastAPI/uvicorn)
     Loaded: loaded; enabled
     Active: active (running) since ... 3s ago
   Main PID: 1670267 (rir2localdb)
     Memory: 72.7M (max: 1.0G available: 951.2M peak: 73.2M)
```

## Проверки

- pytest tests/ — без изменений (151 passed, Stage не трогает тесты).
- ruff/mypy clean.
- `systemd-analyze verify` на 3 юнитах — clean.
- Live smoke на проде: serve.service started, healthz/readyz green,
  memory peak 73MB, JSON-логи в journald.

## Что НЕ сделали

- Reverse proxy (nginx/Caddy) — deployment-specific, вне репо.
- TLS termination — same reason.
- Multi-worker uvicorn — read-heavy API single worker enough.
- Юнит-тест на --help (флаги уже от Stage 1-08, покрыты косвенно).
- Изменения в sync.service.

## Что дальше

Pause или новый followup по запросу. Это была реальная просьба от
первого пользователя — спасибо feedback'у, gap закрыт.
