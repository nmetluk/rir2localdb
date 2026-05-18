# rir2localdb

[![CI](https://github.com/nmetluk/rir2localdb/actions/workflows/ci.yml/badge.svg)](https://github.com/nmetluk/rir2localdb/actions/workflows/ci.yml)

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

# 2. Установить зависимости (рекомендуется venv)
pip install -e ".[dev]"

# 3. Скопировать .env.example → .env и поправить DATABASE_URL под локальную PG
cp .env.example .env

# 4. Накатить миграции
rir2localdb migrate

# 5. Первичная синхронизация. Core tier (~100 секунд, ~750k записей):
rir2localdb sync --tier core
# Опционально rich-tier (RPSL дампы + ARIN IRR; ~10-20 минут, ~5M
# объектов inetnum/aut-num/route/...):
rir2localdb sync --tier core --tier rich

# 6. Посмотреть статус
rir2localdb status

# 7. Запустить HTTP API
rir2localdb serve &

# 8. Запросить allocation по IP / ASN
curl -s http://127.0.0.1:8000/v1/ip/8.8.8.8 | python -m json.tool
curl -s http://127.0.0.1:8000/v1/ip/2001:4860:4860::8888 | python -m json.tool
curl -s http://127.0.0.1:8000/v1/asn/15169 | python -m json.tool
curl -s http://127.0.0.1:8000/v1/status | python -m json.tool

# 9. Stage 2: RPSL-обогащение (inetnum + organisation) — после
#    `rir2localdb sync --tier rich` (см. ADR-0006). Ответ содержит
#    блок `rpsl` с netname/org_name/abuse_c/etc:
curl -s http://127.0.0.1:8000/v1/ip/193.0.6.139 | python -m json.tool
curl -s http://127.0.0.1:8000/v1/asn/3333 | python -m json.tool

# Опционально отключить RPSL-обогащение для bandwidth-sensitive клиентов:
curl -s 'http://127.0.0.1:8000/v1/ip/8.8.8.8?include_rpsl=false' | python -m json.tool

# OpenAPI docs:
# http://127.0.0.1:8000/docs
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

## Development workflow

Проект ведётся двумя Claude'ами через публичный git — планирующий
в чате, исполняющий в Claude Code. Правила, артефакты и жизненный
цикл шага — в [`.claude/WORKFLOW.md`](.claude/WORKFLOW.md). См. также
ADR-0006 в [`docs/09-decisions.md`](docs/09-decisions.md).

## Лицензия

MIT — см. [`LICENSE`](LICENSE).
