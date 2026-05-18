"""Stdlib logging setup для CLI / orchestrator.

Stage 1: plain-text формат, INFO по умолчанию. Stage 3 ops переключит
на структурированный JSON через structlog без изменений в upstream
коде (structlog рендерит поверх stdlib LogRecord'ов).

Публичный API:

    configure_logging(level="INFO", json=False) -> None
"""

from __future__ import annotations

import logging


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    """Настроить root logger.

    Args:
        level: ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR`` / ``CRITICAL``.
            Регистр не важен. Неизвестное значение → ``INFO``.
        json: placeholder для Stage 3. Сейчас игнорируется — формат
            всегда plain text. Параметр оставлен в сигнатуре, чтобы
            CLI / orchestrator уже передавали флаг и переключение
            на structlog в Stage 3 не требовало правки call-sites.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
