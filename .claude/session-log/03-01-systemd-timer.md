# Stage 3-01: systemd timer + service для daily sync

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:**
- `feat(deploy): systemd unit + timer для daily rir2localdb sync` (включил install-script — git stage всё-таки подобрал его)
- `ci: lint systemd units via systemd-analyze`
- `docs: Stage 3-01 deployment via systemd`
- `docs(session-log): 03-01 systemd timer + live install`
- `chore(context): step 3-01 closed`

(Планировал 5 коммитов; install-script слили в первый — он уже был
``--chmod=+x`` в индексе, и ``git add`` для unit'ов подобрал и его.
Содержательно эквивалентно: unit'ы + script — один логический
deploy-block.)

## Что сделано

### Unit-файлы — `deploy/systemd/`

- **`rir2localdb-sync.service`** (oneshot, ~50 строк):
  - User/Group `rir2local`, WorkingDirectory `/home/rir2local/rir2localdb`.
  - `ExecStart=/home/rir2local/rir2localdb/.venv/bin/rir2localdb sync
    --tier core --tier rich`.
  - **Sandbox**: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
    `ProtectHome=read-only` + `ReadWritePaths=/home/rir2local/
    rir2localdb/data` (cache writable, всё остальное home read-only),
    `ProtectKernelTunables/Modules/ControlGroups`, `RestrictAddress
    Families=AF_INET AF_INET6 AF_UNIX`, `RestrictNamespaces`,
    `MemoryDenyWriteExecute`, `RestrictRealtime`, `RestrictSUIDSGID`,
    `LockPersonality`, `SystemCallArchitectures=native`.
  - **Resources**: `TimeoutStartSec=2h`, `MemoryMax=4G`.
  - `ConditionPathExists` на venv binary — sanity-check.
  - `Documentation=https://github.com/nmetluk/rir2localdb`.

- **`rir2localdb-sync.timer`** (~15 строк):
  - `OnCalendar=*-*-* 03:00:00 UTC`,
  - `Persistent=true` (catch-up после downtime),
  - `RandomizedDelaySec=15min` (anti thundering herd на RIR mirrors).

### `scripts/install-systemd.sh`

Executable (0755). Проверяет sudo, копирует unit'ы в
`/etc/systemd/system/`, прогоняет `systemd-analyze verify` перед
`daemon-reload + enable --now`, печатает `list-timers`.

### CI lint

Новый step `lint systemd units` в `ci.yml` после mypy. На runner'е
нет user'а `rir2local` — поэтому sed-копия с dummy `User=root`,
без `ConditionPathExists`, `ExecStart=/bin/true`. Ловит синтакс-ошибки
в PR до того, как они попадут в `install-systemd.sh` на проде.

CI на `2652f60` — `lint systemd units` ✅.

### Документация

- `docs/07-operations.md` § «Daily sync via systemd» — установка,
  расписание, hardening (объяснение каждой sandbox-опции), команды
  эксплуатации (status / list-timers / start / journalctl / edit /
  disable). Stale «Вариант A: systemd» удалён, переделан в реальный
  primary path. cron и Docker оставлены как альтернативы.
- `README.md` Quick start — секция «Production deployment (Linux +
  systemd)» после CLI шагов.

## Live install + verification

```
$ sudo bash scripts/install-systemd.sh
installed: /etc/systemd/system/rir2localdb-sync.service
installed: /etc/systemd/system/rir2localdb-sync.timer
Created symlink ... → /etc/systemd/system/rir2localdb-sync.timer
Установлено и активировано. Текущее расписание:
NEXT                         LEFT LAST PASSED UNIT                   ACTIVATES
Tue 2026-05-19 06:04:40 EEST  11h -         - rir2localdb-sync.timer rir2localdb-sync.service
```

`NEXT 06:04:40 EEST` = `03:04:40 UTC` = `03:00 UTC` + 4 минуты
случайной задержки (`RandomizedDelaySec=15min`). Корректно.

### Manual smoke run

`sudo systemctl start --no-block rir2localdb-sync.service`:
- ✅ Сервис стартовал сразу, без ошибок sandbox.
- ✅ HTTP-запросы к RIR mirrors (afrinic/apnic/...) пошли через 2 сек.
- ✅ Логи через journald (`journalctl -u rir2localdb-sync.service -f`).
- ✅ Memory peak: 235.8M (Stage 2 был ~1GB, на этот раз меньше потому
  что большинство файлов unchanged через md5/HEAD detection).

**Sandbox observations:** ничего не пришлось ослаблять.
- DNS через `RestrictAddressFamilies=AF_INET AF_INET6` работает
  (httpx ходит к RIR'ам).
- Postgres через TCP к localhost тоже OK.
- `MemoryDenyWriteExecute` — asyncpg/Cython не JIT'ят, OK.
- `ProtectHome=read-only` + `ReadWritePaths=/home/rir2local/rir2localdb/data`
  — sync пишет только в `./data` (relative, от WorkingDirectory),
  cache-файлы создаются нормально.

## Проверки

- pytest tests/ — без изменений (132 passed, 1 deselected).
- ruff/mypy clean — без изменений.
- `systemd-analyze verify` на оба unit'а — clean (warning про
  `xray.service` от соседнего systemd unit'а на машине, не наш).
- CI на коммите `2652f60` — все шаги ✅, включая новый `lint systemd
  units`.

## Что НЕ сделали в 3-01

- Prometheus exporter — Stage 3-02.
- Structured JSON logs — Stage 3-02 (сейчас plain-text idiomatic
  через journald).
- Alerting на failures — Stage 3-06 (Grafana / alertmanager).
- Docker / K8s манифесты — Stage 3-04.

## Что дальше

Stage 3-02: Prometheus `/metrics` endpoint + structured JSON logs.
