"""Logging setup для CLI / orchestrator / API.

Два режима через ``Settings.log_format``:

- ``console`` (default) — human-readable, color-less, для интерактивной
  разработки и journald (journald не любит ANSI escapes).
- ``json`` — single-line JSON per event, для production-парсинга через
  ``jq`` / Loki / Elastic. Включается через ``RIR2LOCALDB_LOG_FORMAT=json``.

Реализация через ``structlog``: общий рендеринг pipeline для structlog
loggers и для stdlib loggers (httpx / asyncpg / sqlalchemy). Stdlib
LogRecord'ы конвертируются в structlog event_dict через
``ProcessorFormatter`` — это standard recipe из structlog docs.

Публичный API:

    configure_logging(level="INFO", json_format=False) -> None
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", json_format: bool = False) -> None:
    """Настроить root logger + structlog.

    Args:
        level: ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR`` / ``CRITICAL``.
            Регистр не важен. Неизвестное значение → ``INFO``.
        json_format: ``False`` (default) — console renderer без цветов.
            ``True`` — JSON renderer (single-line JSON per event).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # Shared processors применяются и к structlog, и к stdlib logging.
    # ``merge_contextvars`` — для contextvars-based context binding
    # (например, ``run_id`` в orchestrator scope'е).
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any
    if json_format:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    # structlog configuration — для structlog.get_logger().
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Stdlib logging — выход через тот же renderer.
    # ``ProcessorFormatter`` берёт LogRecord, оборачивает в event_dict,
    # прогоняет через ``foreign_pre_chain`` (shared processors) и
    # финальный renderer.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)
