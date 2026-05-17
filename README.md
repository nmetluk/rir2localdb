# rir2localdb

Утилита, которая ежедневно зеркалит публичные данные пяти RIR
(AFRINIC, APNIC, ARIN, LACNIC, RIPE NCC), парсит их в PostgreSQL
и отдаёт whois-подобную информацию по IP и ASN через REST API.

```
┌────────────────┐    ┌──────────┐    ┌──────────┐    ┌─────┐
│ ftp.<rir>.net  │ →  │  sync    │ →  │ parsers  │ →  │ DB  │
│  (HTTPS)       │    │ fetcher  │    │  ETL     │    │ PG  │
└────────────────┘    └──────────┘    └──────────┘    └──┬──┘
                                                         │
                                                  ┌──────▼──────┐
                                                  │ FastAPI     │
                                                  │ /ip /asn    │
                                                  └─────────────┘
```

## Быстрый старт

```bash
# 1. Поднять PostgreSQL локально
docker compose up -d postgres

# 2. Установить зависимости
pip install -e ".[dev]"

# 3. Накатить миграции
alembic upgrade head

# 4. Запустить первичную синхронизацию (только delegated stats — быстро, ~30 сек)
rir2localdb sync --tier core

# 5. Запустить API
rir2localdb api  # http://localhost:8000/docs
```

## Документация

- [`docs/00-overview.md`](docs/00-overview.md) — цели, область применения, не-цели
- [`docs/01-data-sources.md`](docs/01-data-sources.md) — что и откуда качаем (по каждому RIR)
- [`docs/02-architecture.md`](docs/02-architecture.md) — компоненты, поток данных, стек
- [`docs/03-database-schema.md`](docs/03-database-schema.md) — схема БД и индексы
- [`docs/04-sync-pipeline.md`](docs/04-sync-pipeline.md) — обход, детекция изменений
- [`docs/05-parsers.md`](docs/05-parsers.md) — форматы и парсеры
- [`docs/06-api.md`](docs/06-api.md) — REST API контракт
- [`docs/07-operations.md`](docs/07-operations.md) — деплой, расписание, мониторинг
- [`docs/08-roadmap.md`](docs/08-roadmap.md) — этапы и прогресс
- [`docs/09-decisions.md`](docs/09-decisions.md) — лог архитектурных решений

## Возобновление работы

Если работу прервали — открой [`CONTEXT.md`](CONTEXT.md). Там в одном
месте: где мы сейчас, что сделано, что следующее, известные блокеры.

## Лицензия

MIT — см. [`LICENSE`](LICENSE).
