# Stage 1, шаг 8: FastAPI `/v1/*` endpoints — финальный шаг Stage 1

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:** `3a296e9` (app factory + schemas), `9647166`
(ip/asn routers), `ed2d2bf` (meta router), `8e4e60e` (cli serve),
`fd85c65` (12 API smoke tests + conftest cleanup),
`44fb33c` (parser fix: "2.3" version-header), `8cbaa90` (README + roadmap)

## Что сделано

- **`src/rir2localdb/api/app.py`** — `make_app(settings=None) -> FastAPI`
  фабрика. Lifespan создаёт `AsyncEngine` + `async_sessionmaker`
  на startup, dispose на shutdown. Роутеры подключены под префиксом
  `/v1`. Корень `/` отдаёт JSON-манифест (name, version, docs link,
  список endpoint'ов).
- **`src/rir2localdb/api/schemas.py`** — 4 Pydantic-модели:
  `IpLookupResponse`, `AsnLookupResponse`, `StatusResponse`,
  `HealthzResponse`. Плоские, без обёрток.
- **`/v1/ip/{addr}`** (commit `9647166`) — парсит IPv4/IPv6 через
  `ipaddress.ip_address`, валидирует, выбирает SQL по семейству.
  IPv6 как `Decimal(int(ip))` → numeric (не помещается в int8).
  Узкая запись через `ORDER BY upper-lower ASC LIMIT 1`.
- **`/v1/asn/{num}`** — int path param, валидация 0..2^32-1.
  Один SQL `WHERE asn_range @> CAST(:asn AS int8)`.
- **`/v1/healthz`** — статичный `{"status": "ok"}` без DB.
- **`/v1/status`** — последний sync_run + список sync_file +
  `db_alive`. DB-ошибки → пустой payload + `db_alive: false`,
  HTTP 200 (для liveness стабильности).
- **`rir2localdb serve [--host --port]`** (commit `8e4e60e`) —
  uvicorn-runner. Lazy import чтобы `--help` был быстрый.
- **12 API smoke-сценариев** (commit `fd85c65`) через
  `httpx.ASGITransport` + `app.router.lifespan_context`.
  `clean_db`-фикстура перенесена из test_orchestrator.py в conftest.py
  для shared использования. Pre-population через `_seed_ipv4`/
  `_seed_ipv6`/`_seed_asn` helpers.
- **Bug fix по дороге** (commit `44fb33c`): version-header detection
  расширен с `parts[0].isdigit()` на `re.compile(r"^\d+(\.\d+)*$")`.
  Live Stage 1 sync обнаружил, что APNIC/ARIN/LACNIC перешли на
  формат «2.3» — старый `isdigit()` пропускал version-line в
  основной путь, тот шёл по unknown-type warning. Три WARNING'а
  на каждый run. Fix + новый unit-тест `test_skips_version_header_dotted`.

## Stage 1 DoD — выполнен (live прогон)

```
$ rir2localdb migrate
alembic upgrade head — done

$ rir2localdb sync --tier core
sync_run id=1 status=success
  files: total=5 new=5 updated=0 unchanged=0 errored=0
  parser: records_total=760029
  etl ip:  inserted=646765 updated=0
  etl asn: inserted=113264 updated=0
  duration: 102239 ms
```

API ответы (запись `start` = первый адрес блока):

| Endpoint | RIR / cc | start | value / count | allocated_on |
|---|---|---|---|---|
| `GET /v1/ip/8.8.8.8` | arin / US | 8.8.8.0 | 256 | 2023-12-28 |
| `GET /v1/ip/2001:4860:4860::8888` | arin / US | 2001:4860:: | 32 | 2005-03-14 |
| `GET /v1/asn/15169` | arin / US | 15169 | 1 (status=assigned) | 2000-03-30 |
| `GET /v1/healthz` | — | — | — | `{"status": "ok"}` |
| `GET /v1/status` | — | last_run + 5 sources, db_alive=true | — | — |

Все три Google-ресурса возвращают ARIN-US — реальное состояние
delegated stats. 8.8.8.8 находит не /9 (большой ARIN-блок), а
именно 8.8.8.0/24 — `ORDER BY upper-lower ASC LIMIT 1` работает.

## Проверки

- `pytest tests/` — **67 passed, 1 deselected in 9.00s**
  (9 fetcher + 10 state + 18 parser + 13 etl + 5 orchestrator + 12 api).
- `pytest -m integration tests/integration/` — **1 passed**
  (живой fetch ftp.ripe.net).
- `ruff check src/ tests/ migrations/` — clean.
- `mypy src/ tests/` — clean (35 source files).
- Live Stage 1 DoD curl-checks — все 4 endpoint'а отвечают корректно.

## Решения по ходу

- **`request.app.state.sessionmaker`** вместо `Depends`-инъекции —
  следую промпту пользователя. Альтернатива (FastAPI Depends) была
  бы каноничнее, но добавляет шаблон без выгоды для Stage 1.
- **IPv6 как `Decimal(int(ip))`** — целое 128-битное значение не
  помещается в int8. Передача как Decimal заставляет asyncpg
  сериализовать в numeric. `CAST(:ip AS numeric)` redundant с
  Decimal-параметром, но явно подтверждает планировщику.
- **`host(start_text) AS start`** в SQL — `INET`-cast в text может
  вернуть `/32` суффикс; `host()` явно отдаёт только адрес.
- **API `/v1/status` exception-handler возвращает 200 + db_alive=false** —
  ошибка БД на status-endpoint не должна валить liveness в
  оркестраторе/k8s. Pessimistic readiness keys on `db_alive`.
- **`asyncpg.AmbiguousParameterError`** на одинаковом `$N` для
  разных колонок (smallint + bigint) — пришлось дублировать
  параметр в `_seed_ipv6` (prefix передаётся дважды для
  prefix_length и value). Это специфика asyncpg's type-inference;
  не баг тестов.
- **`clean_db` фикстура переехала в conftest.py** — изначально была
  локальной в test_orchestrator.py, теперь shared между orchestrator-
  и api-тестами. Test_orchestrator.py освобождён от дублирующей
  декларации.

## Открытые вопросы для следующих шагов

- **CI workflow** — не сделан в Stage 1, перенесён в follow-up
  (GitHub Actions: ruff + mypy + pytest + alembic upgrade/downgrade).
- **Per-RIR breakdown в `/v1/status`** — сейчас отдаём список
  всех sources, в будущем удобно агрегировать по rir+tier с подсчётом
  записей.
- **`/v1/readyz`** — отдельный readiness endpoint, отделённый от
  liveness `/v1/healthz`. Сейчас readiness можно вывести из
  `/v1/status.db_alive`, отдельный endpoint — косметика.

## Что дальше

- Stage 1 закрыт. Полный итог — `.claude/session-log/01-99-stage-1-closed.md`.
- Stage 2: RPSL rich-tier (RIPE / APNIC / AFRINIC inetnum + organisation +
  abuse-c). См. `docs/08-roadmap.md` § Stage 2.
- Перед стартом Stage 2 — пройти follow-up: CI workflow в `.github/workflows/`.
