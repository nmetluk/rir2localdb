"""Сценарии ``etl.delegated_etl.apply_delegated_etl`` — стабы (шаг 6).

10 сценариев фиксируют контракт ETL до реализации. БД-операции идут
через ``pg_conn`` (raw asyncpg) и ``pg_sync_run_id`` из ``conftest.py``;
обе фикстуры — на одной транзакции с rollback'ом в teardown.

После реализации (следующий коммит) все 10 должны быть зелёными.
"""

from __future__ import annotations

import asyncpg


async def test_empty_input_no_changes(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Пустой Iterator → 0 rows в обеих таблицах, EtlStats со всеми нулями."""
    raise NotImplementedError("empty input — stage 1 step 6 impl")


async def test_three_ipv4_records_inserted_with_correct_ranges(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Три ipv4 записи → 3 строки в ``ip_allocation``, family=4,
    ``range_v4 = [start_int, start_int + value)`` для каждой;
    ``range_v6 IS NULL``, ``prefix_length IS NULL``."""
    raise NotImplementedError("ipv4 ranges — stage 1 step 6 impl")


async def test_three_ipv6_records_inserted_with_correct_ranges(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Три ipv6 записи → family=6, ``range_v6 = [start_int,
    start_int + 2**(128-value))``, ``prefix_length == value``,
    ``start_text`` — canonical compressed form."""
    raise NotImplementedError("ipv6 ranges — stage 1 step 6 impl")


async def test_three_asn_records_inserted_with_correct_ranges(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Три asn записи → 3 строки в ``asn_allocation``,
    ``asn_range = [start, start + count)``, ``start_asn`` и ``count``
    отдельными колонками."""
    raise NotImplementedError("asn ranges — stage 1 step 6 impl")


async def test_mixed_input_split_correctly(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Смешанный input (ipv4 + ipv6 + asn) → корректно разнесено по
    двум таблицам; ``EtlStats.ip_records`` / ``asn_records`` совпадают
    с входным распределением; ``records_seen`` == сумма; ``skipped == 0``."""
    raise NotImplementedError("mixed input — stage 1 step 6 impl")


async def test_rerun_same_data_updates_last_seen_run(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Два вызова с одним набором записей и разными run_id →
    row count тот же; ``last_seen_run == run_id2``,
    ``first_seen_run == run_id1`` (preserved); EtlStats второго
    вызова: ``ip_inserted == 0``, ``ip_updated == N``."""
    raise NotImplementedError("rerun same data — stage 1 step 6 impl")


async def test_rerun_changed_status_updates_status(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Та же запись (rir, family, start_text, value), но ``status``
    изменился: ``'allocated'`` → ``'reserved'``. В БД новое значение,
    ``first_seen_run`` preserved, ``last_seen_run`` обновлён."""
    raise NotImplementedError("status change — stage 1 step 6 impl")


async def test_rerun_changed_value_creates_new_row(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """Тот же ``(rir, family, start_text)``, но другое ``value`` →
    две строки в БД (natural key включает value). Старая остаётся
    со старым ``last_seen_run``, новая — со свежим. Это и есть
    «изменение размера блока = новая аллокация»."""
    raise NotImplementedError("value change → new row — stage 1 step 6 impl")


async def test_unaligned_ipv4_value_preserved(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """``value=20480`` (не степень двойки) →
    ``upper(range_v4) - lower(range_v4) == 20480``. ETL не «округляет»
    до /20 — хранит как есть, согласно реальности delegated stats."""
    raise NotImplementedError("unaligned ipv4 — stage 1 step 6 impl")


async def test_gist_lookup_finds_inserted_row(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """После INSERT ``8.0.0.0/9``, запрос
    ``SELECT … WHERE range_v4 @> 134217729::int8`` (= 8.0.0.1)
    находит ту самую строку. Проверка end-to-end: GiST-индекс
    + наши ranges + asyncpg-сериализация."""
    raise NotImplementedError("gist lookup — stage 1 step 6 impl")
