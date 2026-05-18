# Stage 3-04: Docker image + compose

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:**
- `feat(docker): Dockerfile multi-stage build (python:3.12-slim)`
- `feat(docker): docker-compose with api + postgres + migrate + sync services`
- `feat(docker): dev override for hot-reload and human logs`
- `ci: docker image build + healthz smoke`
- `docs: production deployment via Docker compose`
- `docs(session-log): 03-04 docker`
- `chore(context): step 3-04 closed`

## Что сделано

### `Dockerfile` (multi-stage)

- **Stage 1 (builder)**: `python:3.12-slim` + `build-essential` +
  `libpq-dev`. Создаёт wheel и устанавливает в `/opt/venv`.
- **Stage 2 (runtime)**: `python:3.12-slim` + `libpq5` +
  `ca-certificates`. Копирует `/opt/venv` из builder.
- **Non-root** user `rir2local` (UID/GID 1001).
- **Default env**: `RIR2LOCALDB_LOG_FORMAT=json`, `LOG_LEVEL=INFO`,
  `DATA_DIR=/var/lib/rir2localdb/data` — production-ready.
- **HEALTHCHECK** через `httpx.get('/v1/healthz', timeout=3)` —
  python-based, без shell-зависимостей.
- **ENTRYPOINT=rir2localdb**, **CMD=serve --host 0.0.0.0 --port 8000**.
  Один image, три роли через CMD override: `serve` / `migrate` /
  `sync`.

### `.dockerignore`

Исключает `.git/`, `.github/`, `.claude/`, `tests/`, `docs/`,
`deploy/`, `scripts/`, `.env*`, `data/` — минимальный build context.

### `docker-compose.yml`

4 services:

- **postgres** — `postgres:16-alpine`, healthcheck, persistent volume
  `rir2localdb-pg-data`. БЕЗ exposed port (только internal network);
  для host-доступа есть dev override.
- **api** — `rir2localdb:latest`, ports `8000:8000`, volume
  `rir2localdb-data` (file cache), `restart: unless-stopped`.
  Depends on postgres health.
- **migrate** — oneshot, `command: ["migrate"]`, `profiles: [migrate]`.
  Не стартует на `up` — явный `docker compose run --rm migrate`.
- **sync** — oneshot, default `command: sync --tier core --tier rich`,
  `profiles: [sync]`. Запускается из host cron / k8s CronJob:
  `docker compose run --rm sync`.

### `docker-compose.dev.yml` (override)

- Postgres exposed на host 5432.
- API: `LOG_FORMAT=console`, `LEVEL=DEBUG`, src/ mounted в venv для
  editable hot-reload без rebuild.

### CI: `docker` job

Новый job в `.github/workflows/ci.yml`:
- `docker build -t rir2localdb:ci .`
- `docker run --rm rir2localdb:ci --help` — image is callable.
- `docker run -d ... -p 8000:8000 -e ...invalid-DB-url` + 10-retry
  curl `/v1/healthz` — проверяет что serve поднимается даже без БД
  (liveness, не readiness).

`needs: ci` — docker job ждёт основной CI job. Образ ephemeral,
push в registry — отдельная история (когда понадобится).

### Документация

- `docs/07-operations.md` § «Deployment via Docker» — полное
  описание архитектуры (один image, три CMD), quick start (4
  команды), production patterns (systemd-wrapped, host cron,
  Kubernetes CronJob YAML), таблица volumes, секция про dev
  override.
- `README.md` Quick start — добавлена секция «Production
  deployment (Docker)» с минимальным cookbook.

## Live smoke ✅

### Build

```
$ sudo docker build -t rir2localdb:test .
... build ...
$ sudo docker images rir2localdb:test --format '{{.Size}}'
335MB
```

Initial fail: hatchling validate_fields на `license = { file =
"LICENSE" }` потому что LICENSE не был copy'нут в build context.
Fix: добавлен `COPY ... LICENSE ./` в Dockerfile.

Final image **335MB** (python:3.12-slim base ~50MB + Python deps +
libpq5). Чуть больше ожидаемых 150MB — но slim base ≠ alpine,
и FastAPI/SQLAlchemy/structlog тащат за собой.

### Compose stack

```
$ sudo docker compose up -d postgres
$ sudo docker compose --profile migrate run --rm migrate
{"event": "Running upgrade  -> 0001, initial schema ...", "level": "info", ...}
{"event": "Running upgrade 0001 -> 0002, rpsl_tables ...", ...}
{"event": "Running upgrade 0002 -> 0003, more_rpsl_tables ...", ...}
{"event": "Running upgrade 0003 -> 0004, normalize_rir_ripe_to_ripencc ...", ...}
{"event": "Running upgrade 0004 -> 0005, add_is_stale_columns ...", ...}
alembic upgrade head — done

$ sudo docker compose up -d api
 Container rir2localdb-api Started

$ curl -fsS http://localhost:8000/v1/healthz
{"status":"ok"}
$ curl -fsS http://localhost:8000/v1/readyz
{"status":"ready"}

$ sudo docker compose ps
NAME                   STATUS
rir2localdb-api        Up 5s (health: starting)
rir2localdb-postgres   Up 1m  (healthy)
```

- ✅ migrate в JSON-формате (Stage 3-02 logging работает в Docker).
- ✅ 0001 → 0005 all migrations apply clean на свежем PG.
- ✅ /v1/healthz и /v1/readyz через docker-mapped 8000.
- ✅ Non-root user `rir2local` (UID 1001) — без хаков, всё работает.

### Edge cases пойманные

- **LICENSE missing** в build context — hatchling крашился. Fixed.
- **`profiles:`** для migrate/sync — это compose v2 фича, исключает
  oneshot service'ы из `docker compose up`. Явный вызов через
  `--profile <name>` или `docker compose run`. Чисто и предсказуемо.
- **Volume permissions** — non-root user 1001 пишет в
  `/var/lib/rir2localdb/data` через volume mount. Dockerfile делает
  `chown rir2local:rir2local` на этот путь ДО `USER` switch'а, и
  Docker volume наследует ownership. Сработало без manual fix'ов.

## Проверки

- pytest tests/ — без изменений (145 passed).
- ruff/mypy/CI — clean.
- `docker build` — clean, finalize 335MB.
- compose up postgres → migrate → up api → curl healthz/readyz —
  все зелёные.

## Что НЕ сделали

- Push в `ghcr.io/nmetluk/rir2localdb` — отдельная история с
  permissions / credentials. Делается когда понадобится реальная
  публикация (CI workflow + secrets).
- Kubernetes manifests / Helm chart — пример CronJob YAML в docs
  достаточен; полный chart — overkill для текущей audience.
- distroless / scratch base — slim уже достаточно компактный,
  -100MB не оправданы lifecycle сложностью.
- BuildKit cache mount — `--mount=type=cache` для pip — речь идёт
  о minor build-time optimization для CI, не критично.

## Что дальше

Stage 3-05: RDAP fallback для ARIN ownership (бывший 2-06).
Опциональный, но повышает coverage для ARIN-блоков, для которых
полного RPSL нет.
