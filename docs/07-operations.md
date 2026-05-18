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

## RDAP fallback (Stage 3-05)

ARIN не публикует open bulk RPSL с ownership; для остальных 4 RIR
данные есть. Stage 3-05 закрывает gap через on-demand RDAP fallback:
при отсутствии inetnum/aut_num в bulk-таблицах ARIN-блок дёргается
через `rdap.arin.net/registry/ip/<addr>`, кэшируется в `rdap_cache`
(default TTL 24h), вписывается в `rpsl` блок ответа прозрачно.

**Opt-in через env:**

```bash
RIR2LOCALDB_RDAP_FALLBACK_ENABLED=true
RIR2LOCALDB_RDAP_CACHE_TTL_HOURS=24
RIR2LOCALDB_RDAP_NEGATIVE_CACHE_MINUTES=5
```

Default off — пользователь явно включает.

**Rate-limit policy:** ARIN допускает ~50 req/min. Полагаемся на
429-ответы + negative cache. При попадании на 429 кэшируется negative
TTL=max(5min, Retry-After). Bursting против uncached blocks
естественно дросселит сам себя.

**Мониторинг:**

```promql
# Сколько RDAP запросов за час, разбивкой cache hit/miss / found/notfound
rate(rir2localdb_rdap_lookups_total[1h])

# Размер кэша
rir2localdb_rdap_cache_entries{status="active"}
rir2localdb_rdap_cache_entries{status="expired"}
```

GC автоматически удаляет entries старее 7 дней. Recently-expired
сохраняются для diagnostic.

См. ADR-0009 для rationale + alternatives.

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

**Docker / Kubernetes CronJob** — см. § «Deployment via Docker» ниже.

## Deployment via Docker

Альтернатива systemd: тот же `rir2localdb` запускается в контейнере.
Один image (`rir2localdb:latest` собранный из `Dockerfile`), три
команды через `ENTRYPOINT=rir2localdb`:

- `serve` (default CMD) — long-running uvicorn, restart unless-stopped.
- `migrate` — oneshot, `alembic upgrade head`.
- `sync --tier core --tier rich` — oneshot, run по cron / K8s CronJob.

`docker-compose.yml` определяет три service'а: `postgres`,
`api` (default `up`), `migrate` и `sync` (через `profiles:` —
не стартуют автоматически, явный вызов).

### Quick start

```bash
# 1. PG + apply migrations + поднять API.
docker compose up -d postgres
docker compose run --rm migrate          # один раз, после первого install и каждого alembic-revision push'а
docker compose up -d api

# 2. Smoke — healthz должен ответить 200.
curl -fsS http://localhost:8000/v1/healthz

# 3. One-off sync вручную.
docker compose run --rm sync             # core+rich (default из compose)
docker compose run --rm sync sync --tier core   # только core (override CMD)

# 4. CLI команды через тот же image.
docker compose run --rm sync status --json
docker compose run --rm sync gc --dry-run
```

### Production cron

systemd timer вне контейнера, дёргает `docker compose run`:

```ini
# /etc/systemd/system/rir2localdb-docker-sync.service
[Service]
Type=oneshot
WorkingDirectory=/opt/rir2localdb
ExecStart=/usr/bin/docker compose run --rm sync
```

Или host cron:

```cron
0 3 * * *  cd /opt/rir2localdb && docker compose run --rm sync >> /var/log/rir2localdb-sync.log 2>&1
```

Или Kubernetes:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: rir2localdb-sync
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: sync
              image: rir2localdb:latest
              args: ["sync", "--tier", "core", "--tier", "rich"]
              env:
                - name: RIR2LOCALDB_DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: rir2localdb
                      key: database_url
                - name: RIR2LOCALDB_LOG_FORMAT
                  value: json
```

### Образ

- Base: `python:3.12-slim` (Debian 12 slim, ~50MB).
- Multi-stage: `builder` ставит wheel, runtime копирует `/opt/venv`.
- Runtime deps: `libpq5` + `ca-certificates`. Build deps
  (`build-essential libpq-dev`) только в builder-stage.
- Non-root user `rir2local` (UID 1001).
- Default `RIR2LOCALDB_LOG_FORMAT=json` — production режим. Override
  `RIR2LOCALDB_LOG_FORMAT=console` для отладки.
- Healthcheck — `python -c "httpx.get('/v1/healthz')"`.
  Liveness, не readiness; не пингует БД.
- Финальный размер: ~330 MB.

### Volumes

| Volume | Path | Назначение | Persistent? |
|---|---|---|---|
| `rir2localdb-pg-data` | `/var/lib/postgresql/data` | данные PG | **да** |
| `rir2localdb-data` | `/var/lib/rir2localdb/data` | cache скачанных файлов | опц. (регенерится sync'ом) |

### Dev override

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

`docker-compose.dev.yml` переопределяет:
- Postgres exposed на host 5432 (для DB-GUI/psql с хоста).
- API в `console` log mode + DEBUG.
- src/ mount'ится в `/opt/venv/lib/python3.12/site-packages/rir2localdb`
  для hot-reload (хоть надо ещё `--reload` в uvicorn — но команда
  `serve` не передаёт; для итеративной разработки удобнее запускать
  uvicorn вручную или через `rir2localdb serve` локально).

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

## Prometheus metrics

`GET /v1/metrics` отдаёт метрики в Prometheus exposition format
(`text/plain; version=0.0.4`).

**Список метрик** (см. `src/rir2localdb/api/metrics.py`):

| Метрика | Тип | Labels | Описание |
|---|---|---|---|
| `rir2localdb_last_sync_run_finished_timestamp_seconds` | gauge | — | Unix timestamp последнего finished sync_run |
| `rir2localdb_last_sync_run_duration_seconds` | gauge | — | Длительность последнего sync_run, секунды |
| `rir2localdb_last_sync_run_status` | gauge | — | 1 = success, 0 = failed, -1 = running/unknown |
| `rir2localdb_table_rows` | gauge | `table` | Приблизительное число строк (из `pg_class.reltuples`) |
| `rir2localdb_source_last_fetched_timestamp_seconds` | gauge | `rir`, `kind`, `url` | Когда последний раз успешно скачивали этот источник |
| `rir2localdb_http_requests_total` | counter | `method`, `endpoint`, `status` | HTTP-запросы к API |
| `rir2localdb_http_request_duration_seconds` | histogram | `method`, `endpoint` | HTTP latency, buckets от 5ms до 10s |

`table_rows` использует **approximation** из `pg_class.reltuples`
(быстро — sub-millisecond), а не `SELECT COUNT(*)` (медленно на 5M+
строк). Цена — точность зависит от `ANALYZE`. PostgreSQL autovacuum
запускает ANALYZE автоматически после значимых изменений; для
безопасности можно `VACUUM ANALYZE` после миграций.

`source_last_fetched_timestamp` ограничен ~29 источниками
(`sources.py`) — cardinality explosion невозможна.

`/v1/metrics` сам **не считается** в `http_requests_total` — иначе на
каждый Prometheus scrape счётчик растёт «сам от себя».

**Prometheus scrape config:**

```yaml
scrape_configs:
  - job_name: rir2localdb
    scrape_interval: 30s
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: /v1/metrics
```

**Полезные queries:**

```promql
# Время с последнего успешного sync (если sync ходит ежедневно — >86400 = алерт)
time() - rir2localdb_last_sync_run_finished_timestamp_seconds

# Sync длится дольше обычного (baseline ~8.5 мин incremental, ~24 мин full)
rir2localdb_last_sync_run_duration_seconds > 1800

# Per-table row counts
rir2localdb_table_rows{table="inetnum"}

# Source не обновлялся 2 дня
time() - rir2localdb_source_last_fetched_timestamp_seconds > 172800
```

## Structured JSON logs

`RIR2LOCALDB_LOG_FORMAT=json` (default `console`) переключает все
логи на single-line JSON per event через ``structlog``. Production
systemd unit (см. `deploy/systemd/rir2localdb-sync.service`)
устанавливает `Environment=RIR2LOCALDB_LOG_FORMAT=json`.

**Формат события:**

```json
{
  "timestamp": "2026-05-18T19:07:44.123456Z",
  "level": "info",
  "logger": "rir2localdb.sync.orchestrator",
  "event": "sync_run finished",
  "run_id": 2,
  "files_total": 29,
  "files_new": 0,
  "files_updated": 13,
  "duration_ms": 507469
}
```

Все стандартные поля (`timestamp`, `level`, `logger`, `event`) +
произвольные `key=value` через `structlog.contextvars` / kwargs.

**jq-примеры в journald:**

```bash
# WARNING и выше за последний час
journalctl -u rir2localdb-sync --since "1 hour ago" -o cat | \
  jq -c 'select(.level == "warning" or .level == "error")'

# События orchestrator'а текущего run'а
journalctl -u rir2localdb-sync -o cat | \
  jq -c 'select(.logger | startswith("rir2localdb.sync"))'

# Извлечь run-summary в табличку
journalctl -u rir2localdb-sync -o cat | \
  jq -r 'select(.event == "sync_run finished") | [.run_id, .duration_ms, .files_updated] | @tsv'
```

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
