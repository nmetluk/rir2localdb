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
from dataclasses import asdict
from typing import Annotated, Any

import alembic.command
import alembic.config
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rir2localdb.config import Settings, get_settings
from rir2localdb.logging_setup import configure_logging
from rir2localdb.sources import Tier
from rir2localdb.sync.gc import GcStats, run_gc
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
    settings = get_settings()
    configure_logging(level=settings.log_level, json_format=settings.log_format == "json")
    tier_list = tier or [Tier.CORE]
    summary = asyncio.run(run_sync(tier_list, settings, dry_run=dry_run))
    _print_summary(summary, dry_run=dry_run)
    if summary.status == "failed":
        raise typer.Exit(code=1)


@app.command()
def status(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output machine-readable JSON instead of rich tables.",
        ),
    ] = False,
) -> None:
    """Показать последние 5 sync_run и текущее состояние sync_file.

    ``--json`` — machine-readable JSON со схемой
    ``{recent_runs, sources, summary_by_rir, db_alive}``. Совпадает с
    HTTP endpoint'ом ``/v1/status``.
    """
    settings = get_settings()
    # CLI status — без INFO-шума, override level. Формат — как в settings.
    configure_logging(level="WARNING", json_format=settings.log_format == "json")
    payload = asyncio.run(_collect_status(settings))
    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        _render_status_tables(payload)


@app.command()
def migrate(
    revision: Annotated[
        str,
        typer.Option("--revision", "-r", help="Alembic revision (по умолчанию head)."),
    ] = "head",
) -> None:
    """Применить ``alembic upgrade`` до указанной revision."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_format=settings.log_format == "json")
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

    settings = get_settings()
    configure_logging(level=settings.log_level, json_format=settings.log_format == "json")
    # ``log_config=None`` — отключает uvicorn-default dictConfig, который
    # бы перебил наш structlog setup. Без этого JSON-логи теряются в
    # production-режиме (uvicorn вставляет свой formatter на root logger).
    uvicorn.run(make_app(settings), host=host, port=port, log_config=None)


@app.command()
def gc(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Показать что было бы помечено, без записи (rollback в конце).",
        ),
    ] = False,
) -> None:
    """Mark stale records (не появлявшиеся в N последних success-sync'ах).

    Запускается **автоматически** после успешного ``rir2localdb sync``
    в той же транзакции — этой команды обычно не нужно. Используйте её
    для:
    - manual diagnostic через ``--dry-run`` (JSON-сводка без записи);
    - повторного маркировки после изменения ``gc_grace_runs`` в .env.

    Подробности policy — `docs/04-sync-pipeline.md` § «GC and stale records».
    """
    settings = get_settings()
    configure_logging(level=settings.log_level, json_format=settings.log_format == "json")
    stats = asyncio.run(_gc_impl(settings, dry_run=dry_run))
    typer.echo(json.dumps(asdict(stats), indent=2, default=str))
    if dry_run:
        typer.echo("\n[dry-run] no changes persisted.")


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


async def _gc_impl(settings: Settings, *, dry_run: bool) -> GcStats:
    """Open engine + session, ``run_gc`` в её транзакции, commit or rollback.

    Закрывает engine после; sessionmaker per-call (CLI — short-lived).
    """
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            txn = await conn.begin()
            try:
                session_maker = async_sessionmaker(bind=conn, expire_on_commit=False)
                async with session_maker() as session:
                    stats = await run_gc(session, settings)
            finally:
                if dry_run:
                    await txn.rollback()
                else:
                    await txn.commit()
        return stats
    finally:
        await engine.dispose()


async def _collect_status(settings: Settings) -> dict[str, Any]:
    """Собрать payload для ``status``: recent_runs + sources + per-RIR + db_alive.

    Структура соответствует HTTP endpoint'у ``/v1/status``:
    ``{recent_runs, sources, summary_by_rir, db_alive}``. Используется
    и для rich-tables рендера, и для JSON-вывода.

    На любой ошибке (DB недоступна) — ``db_alive=False`` и пустые
    списки, чтобы CLI отрабатывал предсказуемо даже когда PG лежит.
    """
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
                        "SELECT url, rir, kind, last_status, last_fetched_at, "
                        "last_parsed_at, last_size FROM sync_file "
                        "ORDER BY last_fetched_at DESC NULLS LAST"
                    )
                )
            ).all()
            ip_counts = {
                r.rir: r.count
                for r in (
                    await conn.execute(
                        text("SELECT rir, COUNT(*) AS count FROM ip_allocation GROUP BY rir")
                    )
                ).all()
            }
            asn_counts = {
                r.rir: r.count
                for r in (
                    await conn.execute(
                        text("SELECT rir, COUNT(*) AS count FROM asn_allocation GROUP BY rir")
                    )
                ).all()
            }
            fetched_at = {
                r.rir: r.last_fetched_at
                for r in (
                    await conn.execute(
                        text(
                            "SELECT rir, MAX(last_fetched_at) AS last_fetched_at "
                            "FROM sync_file GROUP BY rir"
                        )
                    )
                ).all()
            }
        db_alive = True
    except Exception:
        runs = []
        files = []
        ip_counts = {}
        asn_counts = {}
        fetched_at = {}
        db_alive = False
    finally:
        await engine.dispose()

    recent_runs: list[dict[str, Any]] = []
    for row in runs:
        recent_runs.append(
            {
                "id": row.id,
                "tier": row.tier,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "status": row.status,
                "rpsl_records": _rpsl_records_from_stats(row.stats),
                "error": row.error,
            }
        )

    sources: list[dict[str, Any]] = [
        {
            "url": row.url,
            "rir": row.rir,
            "kind": row.kind,
            "last_status": row.last_status,
            "last_fetched_at": row.last_fetched_at,
            "last_parsed_at": row.last_parsed_at,
            "last_size_bytes": row.last_size,
        }
        for row in files
    ]

    all_rirs = sorted(set(ip_counts) | set(asn_counts) | set(fetched_at))
    summary_by_rir = [
        {
            "rir": r,
            "ip_allocations": ip_counts.get(r, 0),
            "asn_allocations": asn_counts.get(r, 0),
            "last_fetched_at": fetched_at.get(r),
        }
        for r in all_rirs
    ]

    return {
        "recent_runs": recent_runs,
        "sources": sources,
        "summary_by_rir": summary_by_rir,
        "db_alive": db_alive,
    }


def _render_status_tables(payload: dict[str, Any]) -> None:
    """Rich-table вывод payload'а из ``_collect_status``."""
    console = Console()

    runs_table = Table(title="Recent sync_run (last 5)")
    for col in ("ID", "Tier", "Started", "Finished", "Status", "RPSL records", "Error"):
        runs_table.add_column(col)
    for run in payload["recent_runs"]:
        rpsl_total = run.get("rpsl_records")
        runs_table.add_row(
            str(run["id"]),
            run["tier"],
            _fmt_dt(run["started_at"]),
            _fmt_dt(run["finished_at"]),
            run["status"],
            "" if rpsl_total is None else str(rpsl_total),
            (run.get("error") or "")[:60],
        )
    console.print(runs_table)

    files_table = Table(title="sync_file (by last_fetched_at DESC)")
    for col in ("URL", "RIR", "Kind", "Last status", "Fetched at"):
        files_table.add_column(col)
    for src in payload["sources"]:
        files_table.add_row(
            src["url"],
            src["rir"],
            src["kind"],
            src["last_status"],
            _fmt_dt(src["last_fetched_at"]),
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
