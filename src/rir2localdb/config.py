"""Application configuration (pydantic-settings).

Читает переменные окружения с префиксом ``RIR2LOCALDB_`` (см. ``.env.example``).
Поля добавляются по мере появления потребителей — пока здесь только то,
что нужно ``Alembic`` (``database_url``) и ``sync/fetcher`` (``data_dir`` +
``http_*``). Остальные ``RIR2LOCALDB_*`` переменные из ``.env`` игнорируются
без ошибки (``extra='ignore'``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфиг приложения."""

    model_config = SettingsConfigDict(
        env_prefix="RIR2LOCALDB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str
    """Полный SQLAlchemy URL вида ``postgresql+asyncpg://user:pass@host:port/db``."""

    test_database_url: PostgresDsn | None = None
    """URL отдельной БД для интеграционных тестов state.py / etl.
    Если ``None`` — соответствующие тесты пропускаются. Локально по
    умолчанию ``postgresql+asyncpg://rir2localdb:rir2localdb@127.0.0.1:5432/rir2localdb_test``
    (см. ``.env.example``).
    """

    data_dir: Path = Path("./data")
    """Корень для локального кэша скачанных файлов и временных артефактов.
    Fetcher кладёт файлы в ``<data_dir>/cache/<rir>/<filename>``.
    """

    http_timeout: float = 60.0
    """Полный таймаут одного HTTP-запроса в секундах."""

    http_max_connections: int = 10
    """Максимум одновременных соединений в общем ``httpx.AsyncClient``."""

    http_retries: int = 3
    """Сколько раз ретраить один HTTP-запрос на 5xx/429/network errors.
    См. ``_retry_request`` в ``sync/fetcher.py`` про backoff.
    """

    log_level: str = "INFO"
    """``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR`` / ``CRITICAL``.
    Регистр не важен."""

    log_format: Literal["console", "json"] = "console"
    """``console`` — human-readable color-less вывод (по умолчанию,
    подходит для интерактивной разработки и journald). ``json`` —
    single-line JSON per event для production-парсинга через ``jq`` /
    Loki / Elastic.
    """

    gc_grace_runs: int = Field(default=7, ge=1, le=365)
    """Сколько последовательных successful sync_run'ов без появления
    записи нужно, чтобы пометить её ``is_stale = TRUE``. Default 7 ≈
    неделя при daily timer'е. Пережёвывает один failed run, выходной с
    downtime, slowly-rolling RIR snapshot. См. ADR-0008."""

    @field_validator("log_format", mode="before")
    @classmethod
    def _normalize_log_format(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон ``Settings`` для приложения.

    Alembic ``env.py`` импортирует и вызывает эту функцию, чтобы получить URL БД.
    """
    return Settings()  # type: ignore[call-arg]
