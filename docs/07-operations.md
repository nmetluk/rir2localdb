# 07 · Эксплуатация

## CLI status: rich vs JSON

По умолчанию `rir2localdb status` печатает две `rich.Table`
(`Recent sync_run` + `sync_file`) — для человека в терминале.

С флагом `--json` отдаёт machine-readable JSON со схемой
`{recent_runs, sources, summary_by_rir, db_alive}` — структура
совпадает с HTTP endpoint'ом `/v1/status`. Удобно для cron-скриптов,
мониторинга и алертинга:

```bash
# последний run прошёл успешно?
rir2localdb status --json | jq -e '.recent_runs[0].status == "success"'

# RPSL records за последний run
rir2localdb status --json | jq '.recent_runs[0].rpsl_records'

# суммарно записей по RIR
rir2localdb status --json | jq '.summary_by_rir[] | "\(.rir): ip=\(.ip_allocations) asn=\(.asn_allocations)"'
```

DB недоступна — `--json` всё равно отвечает `{... "db_alive": false}`
с exit 0, чтобы скрипт сам решил эскалировать.

## Локальная разработка

```bash
# 1. Postgres
docker compose up -d postgres
# 2. Зависимости
pip install -e ".[dev]"
# 3. Миграции
alembic upgrade head
# 4. Прогон руками
rir2localdb sync --tier core
# 5. API в одно окно, потом curl в другое
rir2localdb api
```

`docker-compose.yml` поднимает только Postgres. Сам сервис и
worker запускаются локально из venv — так быстрее итерировать.

## Production

Два процесса:

1. **api** — `uvicorn rir2localdb.api.app:app --host 0.0.0.0 --port 8000`
   (через gunicorn+uvicorn-worker в проде).
2. **sync worker** — раз в сутки выполняет
   `rir2localdb sync --tier core --tier rich`. Запускается через
   systemd timer; см. §«Daily sync via systemd» ниже.

### Daily sync via systemd

Unit-файлы лежат в `deploy/systemd/` (`rir2localdb-sync.service` +
`rir2localdb-sync.timer`). Установка одной командой:

```bash
sudo bash scripts/install-systemd.sh
```

Скрипт:
- копирует unit'ы в `/etc/systemd/system/`,
- прогоняет `systemd-analyze verify` (sanity-check синтаксиса),
- `daemon-reload` + `enable --now` для timer'а,
- печатает следующее запланированное срабатывание.

**Расписание.** `OnCalendar=*-*-* 03:00:00 UTC` ежедневно, плюс
`RandomizedDelaySec=15min` (anti thundering herd на RIR-зеркалах).
`Persistent=true` — если машина была выключена в 03:00, при загрузке
догонит.

**Hardening.** Service запускается под user `rir2local` с sandbox:
`ProtectHome=read-only` + `ReadWritePaths=/home/rir2local/rir2localdb/data`
(только cache писабельный); `ProtectSystem=strict`, `PrivateTmp=true`,
`MemoryDenyWriteExecute=true`, `RestrictAddressFamilies=AF_INET AF_INET6
AF_UNIX`. `MemoryMax=4G` (Stage 2 peak ~1 GB, 4× запас); `TimeoutStartSec=2h`
(Stage 2 ran 24 мин, 5× запас).

**Команды для эксплуатации:**

```bash
# Статус и расписание
systemctl status rir2localdb-sync.timer
systemctl list-timers rir2localdb-sync.timer

# Manual smoke (один запуск)
systemctl start rir2localdb-sync.service

# Логи в реальном времени
journalctl -u rir2localdb-sync.service -f

# Логи последнего run'а
journalctl -u rir2localdb-sync.service --since "1 hour ago"

# Изменить расписание (создаст override в /etc/systemd/system/<unit>.d/override.conf)
systemctl edit rir2localdb-sync.timer

# Отключить sync (timer останется в системе, не сработает)
systemctl disable --now rir2localdb-sync.timer
```

### Альтернативные варианты запуска

systemd timer выше — рекомендуемый вариант для bare metal. Если он
не подходит:

**cron** (минимальный, без sandbox-hardening):

```cron
0 3 * * * rir2local /home/rir2local/rir2localdb/.venv/bin/rir2localdb sync --tier core --tier rich >> /var/log/rir2localdb/sync.log 2>&1
```

**Docker / Kubernetes CronJob** — образ + манифесты будут в Stage 3-04.

## Переменные окружения

Все в `.env.example` с комментариями. Кратко:

```
RIR2LOCALDB_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/rir2localdb
RIR2LOCALDB_DATA_DIR=/var/lib/rir2localdb
RIR2LOCALDB_LOG_LEVEL=info
RIR2LOCALDB_LOG_FORMAT=json            # 'json' | 'console'
RIR2LOCALDB_HTTP_TIMEOUT=60
RIR2LOCALDB_HTTP_MAX_CONNECTIONS=10
RIR2LOCALDB_ARIN_BULK_KEY=             # пусто = ARIN bulk отключён
RIR2LOCALDB_TIERS_ENABLED=core         # comma-separated: core,rich,arin-rr
```

## Логи

- **Формат:** structlog → JSON в проде, цветной плоский в dev.
- **Поля каждого события:** `timestamp`, `level`, `event`, `run_id`
  (если внутри run'а), `rir`, `source_url`, `bytes`, `duration_ms`.
- **Не пишем PII** (там и так нет ничего, но в логах не должно
  быть полных значений `org-name`, `descr` и пр. — только handle/URL).

## Метрики (Stage 3)

Эндпоинт `/metrics` для Prometheus, минимальный набор:

- `rir2localdb_sync_runs_total{tier,status}`
- `rir2localdb_sync_run_duration_seconds{tier}`
- `rir2localdb_files_fetched_total{rir,status}`
- `rir2localdb_records_total{table}` (gauge, обновляется
  по завершении ETL)
- `rir2localdb_api_requests_total{endpoint,status}`
- `rir2localdb_api_request_duration_seconds_bucket{endpoint,le}`

## Алерты

Минимум для Stage 3:

- run в статусе `failed` или `running > 4h` — критический алерт.
- `last_md5` у источника старше 48 часов — предупреждение
  (возможно, RIR временно не обновляет).
- API healthcheck не отвечает 2 минуты — критический алерт.

## Бэкап и восстановление

- Основной источник истины — публичные данные RIR. Полная
  пересборка БД из нуля занимает ~5–10 минут (Stage 1, только
  delegated) или ~1–2 часа (Stage 2, c RPSL).
- Тем не менее: `pg_dump --format=custom rir2localdb` раз в сутки
  на отдельный том. Это страховка от мисскасстейтментов в
  миграции, а не от потери данных.

## Что считается production-ready (DoD для Stage 3)

- 7 дней без вмешательства, успешный daily run каждые сутки.
- API p95 < 50 мс на realistic нагрузке (~100 RPS).
- Дашборд в Grafana / эквиваленте с базовыми метриками.
- Алерты в инциденты ходят, не теряются.
- Runbook на типичные инциденты в `docs/runbook.md` (будет
  написан в Stage 3, когда инциденты появятся).
