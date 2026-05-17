# Roadmap

Краткий вид. Подробное описание каждого этапа с DoD — `docs/08-roadmap.md`.
Текущая позиция — в `CONTEXT.md`.

## Stage 0 · Bootstrap & planning ✅

Скелет репозитория, документация, каталог источников, ADR.

## Stage 1 · Core sync + minimal API · `next`

- Миграции Alembic, таблицы `sync_*`, `ip_allocation`, `asn_allocation`.
- HTTPS-загрузчик с условным GET и md5-валидацией.
- Парсер delegated-extended (NRO format).
- ETL: `COPY` во временную таблицу + atomic swap.
- CLI `sync` / `status`.
- API `GET /v1/ip/{addr}` и `GET /v1/asn/{num}` с базовыми полями.
- Smoke-тесты, базовый CI.

DoD: на чистой машине сценарий из README.md проходит end-to-end.

## Stage 2 · Rich whois (RPSL dumps)

- Парсер RPSL (общий для RIPE/APNIC/AFRINIC).
- Per-RIR таблицы для inetnum / inet6num / aut-num / organisation /
  route(6) / as-block.
- ETL для split-дампов, gzip-стриминг.
- Расширенный API: объединение delegated stats с RPSL-данными в ответе.
- Опциональный коннектор ARIN Bulk Whois (за API-ключом из env).

DoD: для IP в зоне RIPE/APNIC/AFRINIC ответ содержит netname,
org-name, mnt-by, abuse-c.

## Stage 3 · Operations hardening

- Расписание (systemd timer / cron / встроенный APScheduler).
- Структурированные логи (structlog), метрики Prometheus.
- Алерты на провальный run, на устаревший md5.
- Бэкап стратегия, восстановление из дампа.
- Docker-образ для production, healthchecks.

DoD: суточный цикл стабильно работает 7 дней без вмешательства.

## Stage 4 · Quality & coverage

- Покрытие парсеров тестами на репрезентативных фрагментах.
- Fuzz/property-based тесты для RPSL.
- Перформанс-бенчмарк lookup'а IP (цель: p95 < 10 ms).
- OpenAPI-схема + клиентский SDK (генерируется из FastAPI).

## Stage 5 · Bonus / nice-to-have

- BGP-данные (RIPE RIS, RouteViews) для обогащения ASN-инфо.
- RDAP-fallback для LACNIC.
- Историческое хранение (партиции по дате run'а).
- GUI-обёртка (минимальный SPA на FastAPI + HTMX).
