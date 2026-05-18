"""``logging_setup.configure_logging`` — console vs JSON.

Sanity-тесты на два режима. Не покрывают стилистику console-output
(цвет / выравнивание), только инвариант «JSON-режим выводит валидный
JSON, console-режим — нет».
"""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from rir2localdb.logging_setup import configure_logging


def test_json_log_format_produces_single_line_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", json_format=True)
    log = structlog.get_logger("test")
    log.info("hello", key="value", number=42)

    captured = capsys.readouterr()
    line = captured.err.strip()

    # Может быть несколько строк (например, captured handler логирует test setup);
    # берём последнюю — она от нашего log.info("hello").
    json_line = line.splitlines()[-1] if line else ""
    parsed = json.loads(json_line)
    assert parsed["event"] == "hello"
    assert parsed["key"] == "value"
    assert parsed["number"] == 42
    assert parsed["level"] == "info"
    assert "timestamp" in parsed


def test_console_log_format_is_not_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", json_format=False)
    log = structlog.get_logger("test")
    log.info("hello-console", key="value")

    captured = capsys.readouterr()
    line = captured.err.strip()
    assert "hello-console" in line
    # console renderer выдаёт human-readable формат, который НЕ парсится
    # как JSON (минимум потому, что timestamp прямо без кавычек).
    with pytest.raises(json.JSONDecodeError):
        json.loads(line.splitlines()[-1])


def test_stdlib_loggers_share_renderer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stdlib loggers (httpx / asyncpg / sqlalchemy) идут через тот же
    renderer что и structlog — single output format на весь процесс."""
    configure_logging(level="INFO", json_format=True)
    logger = logging.getLogger("rir2localdb.test.stdlib")
    logger.warning("stdlib message")

    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "stdlib message"
    assert parsed["level"] == "warning"
