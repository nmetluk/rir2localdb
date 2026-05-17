# 07 · Эксплуатация

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
2. **sync worker** — раз в сутки выполняет `rir2localdb sync --tier core`
   (плюс `--tier rich` на Stage 2). Запускается через:

### Вариант A: systemd timer (рекомендуется для bare metal)

`/etc/systemd/system/rir2localdb-sync.service`:
```ini
[Unit]
Description=rir2localdb daily sync
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=rir2localdb
EnvironmentFile=/etc/rir2localdb/env
ExecStart=/opt/rir2localdb/.venv/bin/rir2localdb sync --tier core
TimeoutStartSec=2h
```

`/etc/systemd/system/rir2localdb-sync.timer`:
```ini
[Unit]
Description=rir2localdb daily sync

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true
RandomizedDelaySec=600
Unit=rir2localdb-sync.service

[Install]
WantedBy=timers.target
```

### Вариант B: cron

```cron
0 3 * * * rir2localdb /opt/rir2localdb/.venv/bin/rir2localdb sync --tier core >> /var/log/rir2localdb/sync.log 2>&1
```

### Вариант C: Docker / Kubernetes CronJob

В образе есть entrypoint `rir2localdb`. K8s манифест — отдельно
в Stage 3, скелет в `deploy/k8s/` (не в Stage 1).

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
