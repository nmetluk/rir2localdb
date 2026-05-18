# Stage 3-02: Prometheus /metrics + structured JSON logs

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:**
- `feat(deps): add prometheus-client`
- `feat(api): /v1/metrics Prometheus exposition endpoint`
- `feat(api): HTTP request counter + duration histogram middleware`
- `feat(logging): structured JSON logs via structlog`
- `feat(deploy): systemd unit uses RIR2LOCALDB_LOG_FORMAT=json`
- `test: Prometheus metrics + JSON log format`
- `docs: observability — Prometheus scrape and structured logs`
- `fix(sync): clock_timestamp() для finished_at в sync_run`
  (found by live smoke /v1/metrics, см. ниже)
- `docs(session-log): 03-02 metrics + structured logs`
- `chore(context): step 3-02 closed`

## Что сделано

### A. Prometheus `/v1/metrics`

- **`pyproject.toml`** — добавлен `prometheus-client>=0.20`.
- **`src/rir2localdb/api/metrics.py`** — новый модуль, ~230 строк:
  - Module-level `CollectorRegistry` (не дефолтный global — иначе
    state leak между test cases).
  - Counter / Gauge / Histogram (см. таблицу в docs/07).
  - `collect_db_metrics(session)` — 3 SQL запроса на scrape:
    last sync_run, pg_class.reltuples для 15 tracked-таблиц,
    sync_file freshness.
  - `/v1/metrics` через FastAPI router; best-effort при недоступности
    БД (last-known gauges + Prometheus поднимет `up=0`).
- **`src/rir2localdb/api/app.py`** — middleware
  `prometheus_middleware`:
  - инкрементирует `http_requests_total{method, endpoint, status}`,
  - observe'ит `http_request_duration_seconds{method, endpoint}`,
  - **исключает `/v1/metrics`** сам себя — иначе на каждый scrape
    counter растёт «от себя»,
  - `endpoint` = route template (`/v1/ip/{addr}`), не concrete path
    — иначе cardinality explosion.

### B. Structured JSON logs

- **`src/rir2localdb/logging_setup.py`** — полная переписка под
  structlog:
  - `configure_logging(level, json_format)` настраивает один shared
    pipeline процессоров (TimeStamper / add_logger_name / add_log_level
    / merge_contextvars / StackInfoRenderer / format_exc_info).
  - `json_format=True` → JSONRenderer.
  - `json_format=False` → ConsoleRenderer без цветов (journald не любит
    ANSI escapes).
  - stdlib loggers (httpx / asyncpg / sqlalchemy) идут через тот же
    renderer через `ProcessorFormatter` recipe из structlog docs.
- **`src/rir2localdb/config.py`** — `log_level: str` + `log_format:
  Literal["console", "json"]` с case-insensitive validator.
- **`src/rir2localdb/cli.py`** — все CLI commands читают settings и
  передают level + json_format в configure_logging.
- **`deploy/systemd/rir2localdb-sync.service`** — добавлены
  `Environment=RIR2LOCALDB_LOG_FORMAT=json` и `RIR2LOCALDB_LOG_LEVEL=INFO`.

### Tests

- **`tests/test_metrics.py`** (4 cases): exposition format, table_rows
  через pg_class после ANALYZE, http_requests_total инкрементируется,
  `/v1/metrics` само-исключается из counter.
- **`tests/test_logging_setup.py`** (3 cases): JSON валидный single-line
  с полями event/level/timestamp, console-режим не парсится как JSON,
  stdlib loggers разделяют renderer.
- **`tests/conftest.py`** — `api_settings` / `api_client` фикстуры
  перенесены сюда (shared для test_api_smoke + test_metrics).

### Docs

- **`docs/07-operations.md`**:
  - § «Prometheus metrics» — таблица всех метрик, scrape config YAML,
    PromQL примеры (time-since-last-sync / slow-sync / stale-source).
  - § «Structured JSON logs» — формат события, jq-примеры для
    WARNING-фильтра / per-logger / sync-summary TSV-extract.
- **`README.md`** — добавлена секция «Observability» с указателями
  на operations.md.

## Live smoke ✅

### `/v1/metrics` (rir2localdb serve manual)

```
# HELP rir2localdb_last_sync_run_status ...
# TYPE rir2localdb_last_sync_run_status gauge
rir2localdb_last_sync_run_status 1.0
# HELP rir2localdb_table_rows ...
rir2localdb_table_rows{table="mntner"} 41819.0
rir2localdb_table_rows{table="as_set"} 11424.0
rir2localdb_table_rows{table="inetnum"} 6.977018e+06
rir2localdb_table_rows{table="route"} 1.528944e+06
... (15 таблиц)
rir2localdb_source_last_fetched_timestamp_seconds{kind="rpsl-split-gz",rir="ripencc",url="https://ftp.ripe.net/.../inetnum.utf8.gz"} 1.779120012e+09
... (29 источников)
```

Все 15 tracked-таблиц видны, 29 источников с timestamp'ами, sync_run
status=1.0 (success).

### Found bug → fix: `finished_at` = `now()` → `clock_timestamp()`

При первом просмотре /metrics увидел
`rir2localdb_last_sync_run_duration_seconds = 0.0`. Это для sync'а,
который реально шёл 8.5 мин. Причина: в orchestrator'е
`_finalize_sync_run` UPDATE'ит `sync_run SET finished_at = now()` —
а в PostgreSQL `now()` ≡ `statement_timestamp()`, возвращает время
**старта транзакции**, не реальный wall-clock. INSERT sync_run и
UPDATE sync_run в одной транзакции, поэтому started_at = finished_at
до миллисекунды.

Fix: `clock_timestamp()` (commit `3dfca5d`). Старые run'ы 1-3 в БД
имеют duration=0 (исторические данные неисправляемые); следующий
sync будет с корректной длительностью.

### Structured JSON logs in journald

```bash
$ sudo systemctl start --no-block rir2localdb-sync.service
$ sudo journalctl -u rir2localdb-sync.service -o cat | head -5
Starting rir2localdb-sync.service - rir2localdb daily sync (core + rich tiers)...
{"event": "HTTP Request: GET https://ftp.afrinic.net/.../md5 \"HTTP/1.1 200 OK\"", "logger": "httpx", "level": "info", "timestamp": "2026-05-18T16:53:28.960519Z"}
{"event": "HTTP Request: GET https://ftp.apnic.net/.../md5 \"HTTP/1.1 200 OK\"", "logger": "httpx", "level": "info", "timestamp": "2026-05-18T16:53:29.824796Z"}
...
```

✅ Single-line JSON per event, валидные timestamp/level/logger/event
поля. httpx (stdlib logger) идёт через тот же renderer, как и
structlog-loggers. `jq` парсит каждую строку напрямую.

Sync остановлен через `systemctl stop` — не ждал 10+ мин для polного
прогона. Evidence для JSON logs получено в первые 5 строк.

## Проверки

- pytest tests/ — **139 passed, 1 deselected** (132 prior + 4 metrics
  + 3 logging).
- ruff check / format / mypy — clean (47 source files).
- Live `/v1/metrics` отдаёт валидный Prometheus exposition.
- Live JSON-логи в journald парсятся через `jq`.

## Что НЕ сделали

- Grafana dashboard — Stage 3-06.
- Alertmanager rules / Prometheus alert rules — Stage 3-06.
- Distributed tracing (OpenTelemetry) — не нужно сейчас (1 backend
  service, локальная отладка через logs достаточна).
- Custom log aggregation (Loki / Elastic / журналы через rsyslog) —
  это уровнем выше нашего репо.

## Что дальше

Stage 3-03: Stale-records GC (ADR-0001). Cleanup строк с устаревшим
`last_seen_run` после нескольких циклов sync'а.
