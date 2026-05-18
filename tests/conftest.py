"""Pytest-фикстуры уровня всего test-suite'а.

Разделение зон ответственности:

- ``test_settings`` (session) — читает ``RIR2LOCALDB_TEST_DATABASE_URL``
  из env (или ``.env``). Если переменная не задана — тесты, зависящие от
  БД, помечаются ``pytest.skip``. Это позволяет запускать unit-тесты
  fetcher'а / парсеров на CI без поднятой Postgres.
- ``test_engine`` (session, sync) — создаёт ``AsyncEngine`` к тестовой
  БД и прогоняет ``alembic upgrade head`` один раз на весь run.
  Это безопасно делать в синхронном фикстур-сетапе: pytest ещё не
  запустил event loop, ``command.upgrade`` создаёт свой через
  ``asyncio.run`` в env.py.

Два независимых канала к БД:

- **SQLAlchemy путь** (для ``sync/state.py``, OLTP-операции):
  - ``db_session`` (function) — открывает соединение, начинает транзакцию,
    даёт ``AsyncSession`` тесту, в teardown откатывает.
  - ``sync_run_id`` (function) — фабрика на ``db_session``.
- **Raw asyncpg путь** (для ``etl/*``, hot path по ADR-0005):
  - ``pg_conn`` (function) — отдельный ``asyncpg.Connection`` в своей
    транзакции с rollback'ом в teardown. Это другое физическое
    соединение, чем ``db_session``: изменения, видимые одному,
    невидимы другому. Оба независимо откатываются.
  - ``pg_sync_run_id`` (function) — фабрика на ``pg_conn``.

Тест не должен смешивать каналы (не использовать ``db_session`` и
``pg_conn`` одновременно для одного логического тестового сценария) —
из-за разных физических соединений написанное в одном не видно
другому до коммита.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from rir2localdb.config import Settings

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Загрузить ``Settings`` с реальным ``test_database_url``.

    Если ``RIR2LOCALDB_TEST_DATABASE_URL`` не задан ни в env, ни в
    ``.env``, ``test_settings.test_database_url`` будет ``None``.
    Дальнейшие БД-фикстуры в этом случае ``pytest.skip()``.
    """
    # mypy не знает, что database_url подъезжает из env / .env — добавляем ignore.
    return Settings()  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def test_database_url(test_settings: Settings) -> str:
    """Строковая форма ``test_database_url``; skip если не задан."""
    if test_settings.test_database_url is None:
        pytest.skip(
            "RIR2LOCALDB_TEST_DATABASE_URL не задан — пропускаю DB-тесты",
            allow_module_level=False,
        )
    return str(test_settings.test_database_url)


@pytest.fixture(scope="session")
def test_engine(test_database_url: str) -> Iterator[AsyncEngine]:
    """Session-scoped engine + alembic upgrade head на тестовую БД."""
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", test_database_url)
    command.upgrade(cfg, "head")

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        # Engine.dispose — coroutine, но pytest закроет процесс сразу
        # после yield; явный dispose не нужен, asyncpg сам приберёт
        # сокеты при выходе. Если когда-то это станет проблемой —
        # переключимся на async-фикстуру с loop_scope='session'.
        pass


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test ``AsyncSession`` внутри транзакции с rollback'ом в teardown.

    Изоляция тестов: каждый тест видит «пустые» таблицы
    (rollback'ом снимаются все его INSERT'ы).
    """
    async with test_engine.connect() as connection:
        trans = await connection.begin()
        factory = async_sessionmaker(bind=connection, expire_on_commit=False)
        async with factory() as session:
            try:
                yield session
            finally:
                await trans.rollback()


@pytest_asyncio.fixture
async def sync_run_id(db_session: AsyncSession) -> int:
    """Вставить тестовый ``sync_run`` через ``db_session`` и вернуть его ``id``.

    Любая ``sync_file``-строка ссылается на ``sync_run.id`` через FK,
    поэтому для тестов state.py всегда нужен валидный run_id. Для
    ETL-тестов на raw asyncpg есть параллельный ``pg_sync_run_id``.
    """
    result = await db_session.execute(
        text("INSERT INTO sync_run (tier, status) VALUES ('core', 'running') RETURNING id")
    )
    rid = result.scalar_one()
    await db_session.flush()
    return int(rid)


@pytest_asyncio.fixture
async def pg_conn(test_database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """Raw ``asyncpg.Connection`` в транзакции с rollback'ом в teardown.

    Используется ETL-тестами (``apply_delegated_etl`` принимает именно
    ``asyncpg.Connection``, ADR-0005 § hot path). Отдельное физ.соединение
    от ``db_session`` — каждое со своим rollback'ом, без cross-видимости.
    """
    # asyncpg.connect не понимает SQLAlchemy-суффикс '+asyncpg'.
    url = test_database_url.replace("+asyncpg", "")
    conn = await asyncpg.connect(url)
    txn = conn.transaction()
    await txn.start()
    try:
        yield conn
    finally:
        await txn.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def pg_sync_run_id(pg_conn: asyncpg.Connection) -> int:
    """``INSERT … RETURNING id`` в ``sync_run`` через ``pg_conn``.

    Параллель ``sync_run_id``, но в asyncpg-транзакции — видимо для
    ETL-операций, которые тоже идут через ``pg_conn``.
    """
    rid = await pg_conn.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'running') RETURNING id"
    )
    return int(rid)


@pytest_asyncio.fixture
async def clean_db(test_database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """Autocommit-asyncpg conn с ``TRUNCATE … RESTART IDENTITY CASCADE`` до и после теста.

    Используется тестами orchestrator'а и API, где код коммитит свою
    транзакцию (``pg_conn``-фикстура с rollback-в-teardown не подходит,
    потому что её rollback не отменит чужие commit'ы). Альтернатива
    — пара отдельных физ.соединений и truncate-фикстура. Дёшево.

    Tests fixture-уровня очищают и видят коммиченные данные через
    одну и ту же ``clean_db`` connection, либо через свои новые
    подключения (тогда состояние видно через PostgreSQL READ COMMITTED).
    """
    url = test_database_url.replace("+asyncpg", "")
    conn = await asyncpg.connect(url)
    truncate_sql = (
        "TRUNCATE ip_allocation, asn_allocation, sync_file, sync_run RESTART IDENTITY CASCADE"
    )
    try:
        await conn.execute(truncate_sql)
        yield conn
    finally:
        await conn.execute(truncate_sql)
        await conn.close()
