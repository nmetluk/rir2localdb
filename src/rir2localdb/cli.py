"""Typer-based CLI entrypoint.

Stage 1 commands (заполняются по мере готовности соответствующих слоёв,
см. ``docs/08-roadmap.md`` § Stage 1):

    rir2localdb sync [--tier core|rich|arin-rr]
    rir2localdb api
    rir2localdb status
    rir2localdb migrate [up|down]
    rir2localdb gc [--keep N]

Пока — пустой ``typer.Typer()``: ``app`` экспортируется как entry-point
(``pyproject.toml`` ⇒ ``rir2localdb = "rir2localdb.cli:app"``) и ``python
-m rir2localdb`` (``__main__.py``). Команды добавляются ниже, по одной
на pull-request.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="rir2localdb",
    help="Daily mirror of RIR data into PostgreSQL with whois-like REST API.",
    no_args_is_help=True,
)
