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

```
sync_run id=2 status=success
  files: total=29 new=0 updated=13 unchanged=16 errored=0
  parser: records_total=3,592,555
  etl ip:  inserted=8       updated=341,259
  etl asn: inserted=4       updated=47,079
  etl rpsl: records=3,204,205 unknown_type=36,899 malformed=0
           as_set:       inserted=11,424   updated=0
           aut_num:      inserted=1        updated=30,019
           inet6num:     inserted=63       updated=146,890
           inetnum:      inserted=456      updated=1,247,628
           mntner:       inserted=41,819   updated=0
           organisation: inserted=5        updated=21,253
           role:         inserted=1        updated=40,727
           route:        inserted=3,270    updated=946,414
           route6:       inserted=12       updated=677,324
  duration: 507,469 ms (~8.5 минут)
```

- ✅ `code=exited, status=0/SUCCESS`.
- ✅ Memory peak: 235.8M (Stage 2 first-run был ~1GB; меньше потому
  что 16/29 файлов unchanged через md5/HEAD — ETL пропускается).
- ✅ CPU time: 3 min 22 s wall, ~8.5 min total (большая часть — HTTP
  fetch + parsing, ETL UPDATE'ы быстрые).
- ✅ **Новые Stage 2.50 таблицы заполнились:** mntner +41,819,
  as_set +11,424. Это объекты, которые при Stage 2 уходили в
  ``objects_skipped_unknown_type``.
- ✅ unknown_type=36,899 (down from 278k в Stage 2 первом sync'е).
  Остаток — `key-cert`, `domain`, `irt`, `inet-rtr`, `route-set` —
  out-of-scope для нашей RPSL coverage.

Также: `person` пустой в этом sync'е (нет ни insert, ни update).
RIPE `.utf8.gz` дамифицирует PII (`DUMY-RIPE` handles), и судя по
выводу, реальных person-записей в дампах не публикуется — все они
заменены на dummy. Это ожидаемо для PII compliance; контракт
`admin-c="DUMY-RIPE"` → orphan reference сохраняется (фиксируется
как known design в `02-99-stage-2-closed.md`).

**Sandbox observations:** ничего не пришлось ослаблять.
- DNS через `RestrictAddressFamilies=AF_INET AF_INET6` работает
  (httpx ходит к RIR'ам).
- Postgres через TCP к localhost тоже OK.
- `MemoryDenyWriteExecute` — asyncpg/Cython не JIT'ят, OK.
- `ProtectHome=read-only` + `ReadWritePaths=/home/rir2local/
  rir2localdb/data` — sync пишет только в `./data` (relative от
  WorkingDirectory), cache-файлы создаются нормально.
- `PrivateTmp=true` — httpx/asyncpg не используют `/tmp`, OK.

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
