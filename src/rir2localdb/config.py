"""Application configuration (pydantic-settings).

Читает переменные окружения с префиксом ``RIR2LOCALDB_`` (см. ``.env.example``).
В Stage 1 шаг 1 заведено минимум полей — только то, что нужно Alembic'у.
Остальные поля добавляются по мере появления потребителей.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфиг приложения.

    Все поля грузятся из env с префиксом ``RIR2LOCALDB_`` или из ``.env``
    в корне проекта. ``extra='ignore'`` — чтобы переменные из ``.env``,
    для которых здесь ещё нет полей, не вызывали ошибку.
    """

    model_config = SettingsConfigDict(
        env_prefix="RIR2LOCALDB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон ``Settings`` для приложения.

    Alembic ``env.py`` импортирует и вызывает эту функцию, чтобы получить URL БД.
    """
    return Settings()  # type: ignore[call-arg]
