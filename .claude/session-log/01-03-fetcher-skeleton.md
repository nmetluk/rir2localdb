# Stage 1, шаг 3 (часть A): fetcher skeleton — types, contracts, test plan

**Дата:** 2026-05-17
**Статус:** ✅ закрыт
**Коммиты:** `6dfbb16`, `94fc889`, `254cc03`

## Что сделано

- `src/rir2localdb/sync/fetcher.py` — публичная поверхность
  (всё с docstring'ами, тела `raise NotImplementedError`):
  - `FetchStatus(StrEnum)` — `NEW` / `UPDATED` / `UNCHANGED` / `ERROR`.
  - `PreviousFetchState` (dataclass, slots, frozen) —
    `last_md5` / `last_etag` / `last_modified` / `last_sha256`.
  - `FetchResult` — статус, url, local_path, size_bytes,
    content_sha256, etag, last_modified, md5_sidecar, fetch_ms, error.
  - `FetchError(Exception)` — внутреннее, наружу `fetch()` не пробрасывает.
  - `fetch(client, source, previous, settings) -> FetchResult`.
  - Помощники: `make_user_agent`, `make_http_client`, `cache_path_for`,
    `_fetch_md5_sidecar`, `_conditional_get`, `_retry_request`.
  - Sum-type `_ConditionalGetOutcome = _NotModified | _Downloaded`.
- `tests/test_fetcher.py` — 9 сценариев как `raise NotImplementedError`:
  first-fetch / md5-match / 304 / sha-match / sha-diff /
  retry-success / retry-exhausted / 4xx-no-retry / md5-404-graceful.
  Каждый помечает в docstring какой tier должен сработать и какие
  поля `FetchResult` ожидаются.
- `src/rir2localdb/config.py` — `Settings` расширен полями
  `data_dir: Path`, `http_timeout: float`, `http_max_connections: int`,
  `http_retries: int` (всё уже было в `.env.example`).
- `docs/01-data-sources.md` — секция ARIN переписана:
  открытое (delegated, IRR) / открытое-но-не-используем (RPKI, rwhois) /
  закрытое (Bulk Whois). Tier `arin-rr` уточнён —
  только `route`/`route6`/`as-set`/`mntner`.
- `CONTEXT.md` — открытый вопрос #1 переформулирован под гибрид
  IRR + RDAP на Stage 2, Bulk Whois как fallback.

## Проверки

- `ruff format src/ tests/ migrations/` — clean.
- `ruff check src/ tests/ migrations/` — clean.
- `mypy src/ tests/` — `Success: no issues found in 27 source files`.
- pytest пока не запускали — все тела `NotImplementedError`,
  скелет на этом этапе не должен «зеленеть» по тестам.

## Решения по ходу

- **`PreviousFetchState` отдельным типом, не `SyncFile`-ORM-row.**
  Sync-state.py будет читать строку из БД и собирать эту структуру;
  обратно — мапить `FetchResult` на колонки. Тесты fetcher'а вообще
  не трогают БД. Согласовано с пользователем в чате.
- **`make_user_agent` / `make_http_client` / `cache_path_for`
  живут в `fetcher.py`, не в отдельном `sync/http.py`.** Пока
  один потребитель, разнесём при втором.
- **Кэш-layout — `<data_dir>/cache/<rir>/<filename>`,
  overwrite-in-place.** История run'ов — только в БД, per-run
  snapshot на диске не делаем (Stage 3 ops, если понадобится).
- **`_ConditionalGetOutcome` — discriminated union типов**
  (`_NotModified | _Downloaded`), не enum-флаг: разные наборы полей
  естественнее раскладываются через `match` в реализации.
- **Retry без `tenacity`.** Ручной цикл на десяток строк в
  `_retry_request` — пользователь явно попросил не тащить
  зависимость.
- **Backoff-параметры (`2`, `60`) захардкожены в `_retry_request`.**
  В Settings вынесен только `http_retries`. Остальные параметры
  никогда не меняли — добавим в env, если когда-нибудь понадобится.

## Открытые вопросы для следующих шагов

- **HTTP-дата в `last_modified`.** В скелете `PreviousFetchState.last_modified` —
  `str` (значение HTTP-заголовка как есть). В таблице `sync_file.last_modified` —
  `TIMESTAMPTZ`. Конвертация — задача `sync/state.py` (шаг 4), не fetcher'а.
- **Логирование.** `structlog` ещё не настроен (Stage 3 ops). В реализации
  fetcher'а используем stdlib `logging` с именем
  `rir2localdb.sync.fetcher` — заменим на `structlog` без правки кода
  (стандартный stdlib-handler от structlog).

## Что дальше

- Stage 1, шаг 3 (часть B): реализация по скелету.
  - Тривиальные хелперы (UA, client factory, cache path).
  - `_retry_request` с инжектируемым `sleep: Callable`.
  - `_fetch_md5_sidecar` (404 → `None`; парсит `<hash>  <file>` и bare `<hash>`).
  - `_conditional_get` (`client.stream`, `.tmp` → `os.replace`, sha256 на лету).
  - `fetch()` orchestration + edge-case (md5 mismatch + 304 → trust server, warn).
  - 9 тестов на `httpx.MockTransport`, pytest 9/9 green.
- См. `docs/08-roadmap.md` § Stage 1 deliverable «`sync/fetcher.py`».
