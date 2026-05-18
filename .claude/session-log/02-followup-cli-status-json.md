# Stage 2 followup: CLI `status --json` для скриптов и мониторинга

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:** `feat(cli): --json flag for status command`,
`test(cli): status --json output schema`,
`docs: status --json for scripting and monitoring`,
`docs(session-log): 02-followup CLI status --json`,
`chore(context): close CLI machine-readable follow-up`

## Что сделано

- **`rir2localdb status --json`** — новый флаг. При `False` (default)
  поведение не меняется (две `rich.Table`). При `True` — JSON
  через `typer.echo(json.dumps(..., indent=2, default=str))`.
- **Рефакторинг `_print_status` → `_collect_status` + `_render_status_tables`.**
  `_collect_status(settings) -> dict` собирает данные одной транзакцией
  (5 SQL-запросов: recent runs / sources / ip counts / asn counts /
  fetched_at), используется обоими режимами вывода.
- **Схема JSON**: `{recent_runs, sources, summary_by_rir, db_alive}` —
  совпадает с HTTP `/v1/status`. Поле `recent_runs[].rpsl_records`
  достаётся из JSONB-stats через тот же `_rpsl_records_from_stats`.
- **Тест `test_status_json_output_schema`** в `tests/test_cli_status.py`
  через **subprocess**, не `CliRunner`. CliRunner вызывает
  `asyncio.run()` внутри pytest-asyncio loop'а → `RuntimeError: cannot
  be called from a running event loop`. subprocess изолирован,
  заодно покрывает entry-point.
- **Документация**: `docs/07-operations.md` § «CLI status: rich vs
  JSON» с jq-примерами. `README.md` Quick start — шаг 10.

## Проверки

- `pytest tests/` — **127 passed, 1 deselected** (126 prior + 1 новый).
- `ruff check src/ tests/` — clean.
- `ruff format --check src/ tests/` — clean.
- `mypy src/ tests/` — clean (42 source files).
- Live smoke: `rir2localdb status --json` на сервере с полной БД
  отдал валидный JSON, `recent_runs[0].rpsl_records == 9656338`.

## Решения по ходу

### subprocess вместо CliRunner

CliRunner — стандартный typer-helper для тестов, но команда
`status` использует `asyncio.run(_collect_status(...))` внутри.
В pytest-asyncio тестах event-loop уже активен → `asyncio.run`
бросает `RuntimeError`. Варианты:

1. Сделать CLI sync wrapper над async-функцией через `asyncio.new_event_loop()`.
2. Тест через subprocess.

Выбран (2) — minimal изменения в production code, плюс subprocess
проверяет реальный entry-point (`python -m rir2localdb`).

### `db_alive=False` без exception

`_collect_status` обёрнут в try/except — на любой ошибке возвращает
пустые списки и `db_alive=False`, не пробрасывает дальше. Это уже
было в HTTP endpoint'е (см. `api/routers/meta.py`); унифицировано
для CLI. Скрипт всегда получит valid JSON, exit 0 — сам решит
эскалировать через `jq -e '.db_alive'`.

### Поле `last_size_bytes` (не `last_size`)

В JSON-выводе колонка `sync_file.last_size` переименована в
`last_size_bytes` для self-documenting контракта. В rich-table она
не показывалась, поэтому breaking changes нет.

## Что дальше

Пока ничего. Stage 3 ops использует `--json` в systemd-юните или
Prometheus pushgateway без дополнительных правок.
