"""Тесты ``parsers.delegated``: 5 RIR-фрагментов + 9 edge cases.

Реальные сетевые/файловые запросы — нет; парсер чисто in-memory,
тестируем через ``tmp_path``. Фрагменты — синтетические, но в
точности соответствуют NRO pipe-format (см. ``docs/05-parsers.md``).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from rir2localdb.parsers import DelegatedRecord, parse_delegated

# ---------------------------------------------------------------------------
# Реалистичные фрагменты от пяти RIR.
#
# Каждый: version line + 3 summary line + несколько записей. Записи —
# реальные значения первых блоков из публичных delegated-extended (как
# на момент написания теста) либо синтетические, формально валидные.
# ---------------------------------------------------------------------------

FRAGMENT_APNIC = """\
2|apnic|20260517|110000|19830101|20260516|+1000
apnic|*|asn|*|18000|summary
apnic|*|ipv4|*|65000|summary
apnic|*|ipv6|*|95000|summary
apnic|JP|asn|2497|1|19960207|allocated|A91A6BB0|e-stats
apnic|JP|ipv4|10.0.0.0|20480|20080808|allocated|A91A6BB0|e-stats
apnic|CN|ipv4|14.0.0.0|524288|20100630|allocated|A914FB60|e-stats
apnic|JP|ipv6|2001:200::|32|19990813|allocated|A91A6BB0|e-stats
"""

FRAGMENT_ARIN = """\
2|arin|20260517|33000|19820101|20260516|-0400
arin|*|asn|*|29000|summary
arin|*|ipv4|*|45000|summary
arin|*|ipv6|*|6000|summary
arin|US|asn|701|1|19890711|allocated|d9f23aa4-...|
arin|US|ipv4|8.0.0.0|16777216|19921201|allocated|c5a3e0b0|e-stats
arin|US|ipv6|2001:400::|32|19991101|allocated|aaaa1111|e-stats
"""

FRAGMENT_LACNIC = """\
2.3|lacnic|20260517|15000|20020101|20260516|-0300
lacnic|*|asn|*|13000|summary
lacnic|*|ipv4|*|13000|summary
lacnic|*|ipv6|*|12000|summary
lacnic|BR|asn|10881|1|20050615|allocated
lacnic||ipv4|177.0.0.0|65536|20100614|reserved
lacnic|AR|ipv6|2800:40::|32|20081101|allocated
"""

FRAGMENT_RIPENCC = """\
2|ripencc|20260517|180123|19920901|20260516|+0200
ripencc|*|asn|*|36000|summary
ripencc|*|ipv4|*|65500|summary
ripencc|*|ipv6|*|95000|summary
ripencc|DE|asn|196608|1|20070801|allocated|A91C2D00|e-stats
ripencc|FR|ipv4|2.0.0.0|65536|20100712|allocated|A91C2D00|e-stats
ripencc|GB|ipv6|2a00:1450::|32|20080801|allocated|A91C2D00|e-stats
"""

FRAGMENT_AFRINIC = """\
2|afrinic|20260517|7500|20050101|20260516|+0000
afrinic|*|asn|*|2300|summary
afrinic|*|ipv4|*|6500|summary
afrinic|*|ipv6|*|800|summary
afrinic|ZA|asn|36864|1|20050501|allocated|F36000|e-stats
afrinic|EG|ipv4|41.0.0.0|65536|20080424|allocated|F36000|e-stats
afrinic|ZA|ipv6|2c00:f700::|32|20070101|allocated|F36000|e-stats
"""

FRAGMENTS = {
    "apnic": FRAGMENT_APNIC,
    "arin": FRAGMENT_ARIN,
    "lacnic": FRAGMENT_LACNIC,
    "ripencc": FRAGMENT_RIPENCC,
    "afrinic": FRAGMENT_AFRINIC,
}

# Эталоны первой записи каждого фрагмента (плюс ожидаемое количество).
FIRST_RECORD_EXPECTATIONS: dict[str, tuple[int, DelegatedRecord]] = {
    "apnic": (
        4,
        DelegatedRecord(
            registry="apnic",
            cc="JP",
            type="asn",
            start="2497",
            value=1,
            date=date(1996, 2, 7),
            status="allocated",
            opaque_id="A91A6BB0",
            extensions="e-stats",
        ),
    ),
    "arin": (
        3,
        DelegatedRecord(
            registry="arin",
            cc="US",
            type="asn",
            start="701",
            value=1,
            date=date(1989, 7, 11),
            status="allocated",
            opaque_id="d9f23aa4-...",
            extensions=None,
        ),
    ),
    "lacnic": (
        3,
        DelegatedRecord(
            registry="lacnic",
            cc="BR",
            type="asn",
            start="10881",
            value=1,
            date=date(2005, 6, 15),
            status="allocated",
            opaque_id=None,
            extensions=None,
        ),
    ),
    "ripencc": (
        3,
        DelegatedRecord(
            registry="ripencc",
            cc="DE",
            type="asn",
            start="196608",
            value=1,
            date=date(2007, 8, 1),
            status="allocated",
            opaque_id="A91C2D00",
            extensions="e-stats",
        ),
    ),
    "afrinic": (
        3,
        DelegatedRecord(
            registry="afrinic",
            cc="ZA",
            type="asn",
            start="36864",
            value=1,
            date=date(2005, 5, 1),
            status="allocated",
            opaque_id="F36000",
            extensions="e-stats",
        ),
    ),
}


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "delegated.txt"
    p.write_text(content, encoding="ascii")
    return p


@pytest.mark.parametrize("registry", list(FRAGMENTS.keys()))
def test_parse_full_fragment(tmp_path: Path, registry: str) -> None:
    """Каждый фрагмент даёт ожидаемое число записей; первая запись совпадает с эталоном."""
    path = _write(tmp_path, FRAGMENTS[registry])
    records = list(parse_delegated(path))

    expected_count, expected_first = FIRST_RECORD_EXPECTATIONS[registry]
    assert len(records) == expected_count, (
        f"{registry}: expected {expected_count} records, got {len(records)}"
    )
    assert records[0] == expected_first


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_skips_comments(tmp_path: Path) -> None:
    content = """\
# this is a comment line
2|ripencc|20260517|180123|19920901|20260516|+0200
# another comment
ripencc|DE|asn|196608|1|20070801|allocated|A91|e-stats
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].registry == "ripencc"


def test_skips_summary_lines(tmp_path: Path) -> None:
    content = """\
2|apnic|20260517|110000|19830101|20260516|+1000
apnic|*|asn|*|18000|summary
apnic|*|ipv4|*|65000|summary
apnic|*|ipv6|*|95000|summary
apnic|JP|asn|2497|1|19960207|allocated|A91|e-stats
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].type == "asn"


def test_skips_version_header(tmp_path: Path) -> None:
    # Голый version-header без записей даёт пустой итератор.
    content = "2|ripencc|20260517|180123|19920901|20260516|+0200\n"
    records = list(parse_delegated(_write(tmp_path, content)))
    assert records == []


def test_skips_version_header_dotted(tmp_path: Path) -> None:
    # APNIC / ARIN / LACNIC начиная с ~2024 используют "2.3" в version-line.
    # `isdigit()` для "2.3" false — нужен regex `^\d+(\.\d+)*$`.
    content = (
        "2.3|apnic|20260518|184596||20260515|+1000\n"
        "apnic|JP|asn|2497|1|19960207|allocated|A91|e-stats\n"
    )
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].registry == "apnic"


def test_skips_iana_records(tmp_path: Path) -> None:
    content = """\
2|apnic|20260517|110000|19830101|20260516|+1000
iana|ZZ|ipv4|192.0.0.0|256|20100114|ietf
iana|ZZ|asn|0|1|20100114|reserved
apnic|JP|asn|2497|1|19960207|allocated|A91|e-stats
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].registry == "apnic"


def test_handles_crlf(tmp_path: Path) -> None:
    content = (
        "2|ripencc|20260517|180123|19920901|20260516|+0200\r\n"
        "ripencc|DE|asn|196608|1|20070801|allocated|A91|e-stats\r\n"
    )
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    # extensions не должны нести трейлинг \r.
    assert records[0].extensions == "e-stats"


def test_handles_unaligned_ipv4_value(tmp_path: Path) -> None:
    # value=20480 — не степень двойки; парсер не должен ругаться.
    content = """\
2|apnic|20260517|110000|19830101|20260516|+1000
apnic|JP|ipv4|10.0.0.0|20480|20080808|allocated|A91|e-stats
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].value == 20480
    assert records[0].start == "10.0.0.0"


def test_empty_cc_becomes_none(tmp_path: Path) -> None:
    content = """\
2|lacnic|20260517|15000|20020101|20260516|-0300
lacnic||ipv4|177.0.0.0|65536|20100614|reserved
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].cc is None


def test_zz_cc_preserved(tmp_path: Path) -> None:
    # ZZ — валидный NRO "unknown country" токен, не превращается в None.
    content = """\
2|apnic|20260517|110000|19830101|20260516|+1000
apnic|ZZ|ipv4|1.2.3.0|256|20200101|reserved|A91|e-stats
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].cc == "ZZ"


def test_missing_optional_fields(tmp_path: Path) -> None:
    # Reduced extended-stats: 7 полей, без opaque_id и extensions.
    # parts[0] здесь — имя registry (alphabetic), значит НЕ version header.
    content = """\
2|lacnic|20260517|15000|20020101|20260516|-0300
lacnic|BR|asn|10881|1|20050615|allocated
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    rec = records[0]
    assert rec.opaque_id is None
    assert rec.extensions is None
    assert rec.status == "allocated"


def test_invalid_date_raises(tmp_path: Path) -> None:
    content = """\
2|ripencc|20260517|180123|19920901|20260516|+0200
ripencc|DE|asn|196608|1|99999999|allocated|A91|e-stats
"""
    with pytest.raises(ValueError):
        list(parse_delegated(_write(tmp_path, content)))


def test_zero_date_becomes_none(tmp_path: Path) -> None:
    # NRO spec: "00000000" — каноничный «без даты»; не путать с битой.
    content = """\
2|ripencc|20260517|180123|19920901|20260516|+0200
ripencc|DE|ipv4|1.2.3.0|256|00000000|reserved|A91|e-stats
"""
    records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].date is None


def test_unknown_type_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Forward-compat: если NRO добавит новый тип ресурса (например 'ipv7'),
    # парсер пропустит запись и сообщит в лог.
    content = """\
2|apnic|20260517|110000|19830101|20260516|+1000
apnic|JP|ipv7|fe80::|64|20300101|allocated|A91|e-stats
apnic|JP|asn|2497|1|19960207|allocated|A91|e-stats
"""
    with caplog.at_level(logging.WARNING, logger="rir2localdb.parsers.delegated"):
        records = list(parse_delegated(_write(tmp_path, content)))
    assert len(records) == 1
    assert records[0].type == "asn"
    assert any("unknown resource type" in r.message for r in caplog.records)
