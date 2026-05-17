# Contributing

## Структура работы

- **Большие изменения** идут поэтапно по `docs/08-roadmap.md`.
  В конце каждого этапа — обновляется `CONTEXT.md`.
- **Маленькие правки** (баги, опечатки, точечные улучшения) —
  обычным PR без формальностей.

## Стиль

- `ruff check` и `ruff format` должны проходить без правок.
- Имена в коде (модули, функции, переменные, лог-сообщения) — на
  английском. Документация (`docs/`, `README.md`, `CONTEXT.md`,
  ADR) — допускается на русском.
- Docstring'и — на английском, краткие, в стиле numpy/pep257.
- Никаких URL-констант в коде вне `src/rir2localdb/sources.py`.

## Коммиты

Conventional Commits:
```
feat(sync): add md5 sidecar validation
fix(parsers): handle continuation lines starting with '+'
chore(context): update after Stage 1.3
docs(adr): add ADR-0006 about partitioning
```

## Тесты

- На каждый парсер — тест с маленьким реальным фрагментом
  данных в `tests/fixtures/`.
- ETL — тестируется на testcontainers Postgres.
- API — тестируется через `httpx.AsyncClient` к работающему app.

## Когда трогать `CONTEXT.md`

Всегда, если:

- Завершил этап или его подэтап.
- Возник блокер, который не решается в этой сессии.
- Принял архитектурное решение → создал ADR, ссылку в CONTEXT.

## Когда трогать `sources.py`

- Когда у RIR-а изменился путь к файлу. Параллельно — короткая
  запись в `CHANGELOG.md` (создаётся в Stage 2, когда станет о чём
  писать).
