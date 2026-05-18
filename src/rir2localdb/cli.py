"""Typer-based CLI entrypoint.

Commands:
    rir2localdb sync    [--tier core|rich|arin-rr|arin-bulk] [--dry-run]
    rir2localdb status
    rir2localdb migrate [--revision REV]
    rir2localdb gc
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
from typing import Annotated

import alembic.command
import alembic.config
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from rir2localdb.config import Settings, get_settings
from rir2localdb.logging_setup import configure_logging
from rir2localdb.sources import Tier
from rir2localdb.sync.orchestrator import SyncRunSummary, run_sync

app = typer.Typer(
    name="rir2localdb",
    help="Daily mirror of RIR data into PostgreSQL with whois-like REST API.",
    no_args_is_help=True,
)


@app.command()
def sync(
    tier: Annotated[
        list[Tier] | None,
        typer.Option(
            "--tier",
            "-t",
            help="Tier для обработки. Можно указывать несколько раз. По умолчанию: core.",
            case_sensitive=False,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Прогон без записи в БД (финальная транзакция rollback'ается).",
        ),
    ] = False,
) -> None:
    """Запустить один sync-run по заданным тирам."""
    configure_logging()
    settings = get_settings()
    tier_list = tier or [Tier.CORE]
    summary = asyncio.run(run_sync(tier_list, settings, dry_run=dry_run))
    _print_summary(summary, dry_run=dry_run)
    if summary.status == "failed":
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Показать последние 5 sync_run и текущее состояние sync_file."""
    configure_logging(level="WARNING")  # CLI status — без INFO-шума
    settings = get_settings()
    asyncio.run(_print_status(settings))


@app.command()
def migrate(
    revision: Annotated[
        str,
        typer.Option("--revision", "-r", help="Alembic revision (по умолчанию head)."),
    ] = "head",
) -> None:
    """Применить ``alembic upgrade`` до указанной revision."""
    configure_logging()
    settings = get_settings()
    cfg = _alembic_config(settings)
    alembic.command.upgrade(cfg, revision)
    typer.echo(f"alembic upgrade {revision} — done")


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option("--host", help="Адрес bind. По умолчанию 127.0.0.1."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Порт. По умолчанию 8000."),
    ] = 8000,
) -> None:
    """Запустить HTTP API через uvicorn."""
    import uvicorn

    from rir2localdb.api.app import make_app

    configure_logging()
    uvicorn.run(make_app(), host=host, port=port)


@app.command()
def gc() -> None:
    """Cleanup stale rows (placeholder, реализация — Stage 3 ops)."""
    typer.echo(
        "gc: placeholder. Stale-row cleanup is planned for Stage 3 ops; "
        "in Stage 1 stale rows just stay with an older `last_seen_run`."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alembic_config(settings: Settings) -> alembic.config.Config:
    """Собрать ``alembic.config.Config`` без файла ``alembic.ini``.

    ``script_location`` через ``importlib.resources.files("rir2localdb")``
    — миграции лежат внутри пакета (`src/rir2localdb/migrations/`),
    поэтому путь корректен и для ``pip install -e .``, и для
    wheel-установки. CLI работает из любого cwd.
    """
    migrations_dir = importlib.resources.files("rir2localdb").joinpath("migrations")
    cfg = alembic.config.Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def _print_summary(summary: SyncRunSummary, *, dry_run: bool) -> None:
    """Печать сводки sync-run'а в stdout."""
    prefix = "[dry-run] " if dry_run else ""
    typer.echo(f"{prefix}sync_run id={summary.run_id} status={summary.status}")
    typer.echo(
        f"  files: total={summary.files_total} "
        f"new={summary.files_fetched_new} "
        f"updated={summary.files_fetched_updated} "
        f"unchanged={summary.files_unchanged} "
        f"errored={summary.files_errored}"
    )
    typer.echo(f"  parser: records_total={summary.parser_records_total}")
    typer.echo(f"  etl ip:  inserted={summary.etl_ip_inserted} updated={summary.etl_ip_updated}")
    typer.echo(f"  etl asn: inserted={summary.etl_asn_inserted} updated={summary.etl_asn_updated}")
    if summary.etl_rpsl_records_total > 0:
        typer.echo(
            f"  etl rpsl: records={summary.etl_rpsl_records_total} "
            f"unknown_type={summary.etl_rpsl_unknown_type_skipped} "
            f"malformed={summary.etl_rpsl_malformed_skipped}"
        )
        for tbl, counts in sorted(summary.etl_rpsl_by_type.items()):
            typer.echo(
                f"           {tbl}: inserted={counts.get('inserted', 0)} "
                f"updated={counts.get('updated', 0)}"
            )
    typer.echo(f"  duration: {summary.duration_ms} ms")
    if summary.error:
        typer.echo(f"  error: {summary.error}", err=True)


async def _print_status(settings: Settings) -> None:
    """Две таблицы: последние sync_run и sync_file."""
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            runs = (
                await conn.execute(
                    text(
                        "SELECT id, tier, started_at, finished_at, status, error, "
                        "stats FROM sync_run ORDER BY id DESC LIMIT 5"
                    )
                )
            ).all()
            files = (
                await conn.execute(
                    text(
                        "SELECT url, rir, kind, last_status, last_fetched_at "
                        "FROM sync_file ORDER BY last_fetched_at DESC NULLS LAST"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    console = Console()

    runs_table = Table(title="Recent sync_run (last 5)")
    for col in ("ID", "Tier", "Started", "Finished", "Status", "RPSL records", "Error"):
        runs_table.add_column(col)
    for row in runs:
        rpsl_total = _rpsl_records_from_stats(row.stats)
        runs_table.add_row(
            str(row.id),
            row.tier,
            _fmt_dt(row.started_at),
            _fmt_dt(row.finished_at),
            row.status,
            "" if rpsl_total is None else str(rpsl_total),
            (row.error or "")[:60],
        )
    console.print(runs_table)

    files_table = Table(title="sync_file (by last_fetched_at DESC)")
    for col in ("URL", "RIR", "Kind", "Last status", "Fetched at"):
        files_table.add_column(col)
    for row in files:
        files_table.add_row(
            row.url,
            row.rir,
            row.kind,
            row.last_status,
            _fmt_dt(row.last_fetched_at),
        )
    console.print(files_table)


def _fmt_dt(value: object) -> str:
    """``datetime`` → ISO-строка; ``None`` → пустая."""
    if value is None:
        return ""
    return str(value)


def _rpsl_records_from_stats(stats: object) -> int | None:
    """Достать ``etl_rpsl_records_total`` из JSONB stats.

    SQLAlchemy через asyncpg возвращает JSONB как str (без custom
    type codec'а); поэтому сначала парсим JSON если получили str.
    Для старых run'ов без stats или для run'ов без RPSL-полей —
    возвращаем ``None``, чтобы CLI показал пустую ячейку.
    """
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except (ValueError, TypeError):
            return None
    if not isinstance(stats, dict):
        return None
    value = stats.get("etl_rpsl_records_total")
    if not isinstance(value, int) or value == 0:
        return None
    return value
