# 09 · Лог решений

Архитектурные решения хранятся как ADR (Architecture Decision
Records) в `docs/adr/`. Формат — лёгкий: контекст, решение,
следствия. Когда решение меняется — пишем новый ADR со ссылкой на
старый, старый помечаем `superseded`.

## Действующие ADR

- [`adr/0001-language-python.md`](adr/0001-language-python.md) —
  Python 3.12 как основной язык.
- [`adr/0002-storage-postgresql.md`](adr/0002-storage-postgresql.md) —
  PostgreSQL 16+ как хранилище.
- [`adr/0003-range-storage.md`](adr/0003-range-storage.md) —
  `int8range` / `numrange` для IP-диапазонов вместо `cidr`/`inet`.
- [`adr/0004-https-not-ftp.md`](adr/0004-https-not-ftp.md) —
  HTTPS поверх ftp.* хостов, не FTP.
- [`adr/0005-async-sqlalchemy.md`](adr/0005-async-sqlalchemy.md) —
  SQLAlchemy 2.x async + asyncpg, сырой SQL на горячих путях.
- [`adr/0006-cooperative-claude-workflow.md`](adr/0006-cooperative-claude-workflow.md) —
  планирующий Claude в чате + исполняющий Claude Code,
  обмен через публичный git и Telegram-уведомления.

## Когда писать новый ADR

- Меняется выбор технологии (БД, фреймворк API, формат хранения).
- Меняется ключевой инвариант данных (например, политика
  «не удаляем, помечаем» → «удаляем после N run'ов»).
- Принимается компромисс с нетривиальными следствиями
  («не делаем X сейчас, потому что Y»).

## Когда не писать ADR

- Косметика, рефакторинги, переименования.
- Добавление зависимости без архитектурных последствий.
- Изменения только в одном модуле.
