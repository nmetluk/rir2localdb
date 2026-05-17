"""SQLAlchemy 2.x declarative base.

В Stage 1 шаг 1 здесь только ``Base`` — пустой реестр метаданных, на
который ссылается ``migrations/env.py``. Модели таблиц (``SyncRun``,
``SyncFile``, ``IpAllocation``, ``AsnAllocation``) добавляются позже,
когда они понадобятся ORM-слою (sync state, queries). Сама миграция
``0001_initial`` написана руками через ``op.execute``/``sa.Column``,
потому что range-типы и GiST-индексы плохо выражаются через
declarative-mapping.

См. ``docs/03-database-schema.md``.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Корневой declarative-base для всех ORM-моделей проекта."""
