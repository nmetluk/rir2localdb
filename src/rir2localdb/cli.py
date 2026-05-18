"""Typer-based CLI entrypoint.

Commands:
    rir2localdb sync    [--tier core|rich|arin-rr|arin-bulk] [--dry-run]
    rir2localdb status
    rir2localdb migrate [--revision REV]
    rir2localdb gc

Skeleton stage (step 7 phase A): typer-структура и сигнатуры
зафиксированы, тела команд — ``raise NotImplementedError``.
Заполняются в фазе B (см. ``.claude/session-log/01-07b-...``).
"""

from __future__ import annotations

from typing import Annotated

import typer

from rir2localdb.sources import Tier

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
            help="Tier для обработки. Можно указывать несколько раз. "
            "По умолчанию: core.",
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
    raise NotImplementedError("sync command — stage 1 step 7 phase B")


@app.command()
def status() -> None:
    """Показать последние 5 sync_run и текущее состояние sync_file."""
    raise NotImplementedError("status command — stage 1 step 7 phase B")


@app.command()
def migrate(
    revision: Annotated[
        str,
        typer.Option(
            "--revision", "-r", help="Alembic revision (по умолчанию head)."
        ),
    ] = "head",
) -> None:
    """Применить ``alembic upgrade`` до указанной revision."""
    raise NotImplementedError("migrate command — stage 1 step 7 phase B")


@app.command()
def gc() -> None:
    """Cleanup stale rows (placeholder, реализация — Stage 3 ops)."""
    raise NotImplementedError("gc command — stage 3 ops")
