"""Сценарии ``parsers.rpsl.parse_rpsl`` — 18 кейсов.

Все in-memory: write content to ``tmp_path``, parse, assert.
БД и сеть не задействованы.
"""

from __future__ import annotations

import gzip
import logging
from pathlib import Path

import pytest

from rir2localdb.parsers import parse_rpsl, parse_rpsl_with_stats


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "rpsl.txt"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_single_inetnum_object(tmp_path: Path) -> None:
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
netname:        RIPE-NCC
descr:          RIPE Network Coordination Centre
descr:          Amsterdam, NL
country:        NL
admin-c:        BRD-RIPE
tech-c:         OPS4-RIPE
status:         ASSIGNED PA
mnt-by:         RIPE-NCC-MNT
source:         RIPE
"""
    objs = list(parse_rpsl(_write(tmp_path, content)))
    assert len(objs) == 1
    obj = objs[0]
    assert obj["inetnum"] == ["193.0.0.0 - 193.0.0.255"]
    assert obj["netname"] == ["RIPE-NCC"]
    assert obj["descr"] == [
        "RIPE Network Coordination Centre",
        "Amsterdam, NL",
    ]
    assert obj["country"] == ["NL"]
    assert obj["admin-c"] == ["BRD-RIPE"]
    assert obj["source"] == ["RIPE"]


def test_multiple_objects(tmp_path: Path) -> None:
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
netname:        FIRST
source:         RIPE

inetnum:        193.0.1.0 - 193.0.1.255
netname:        SECOND
source:         RIPE
"""
    objs = list(parse_rpsl(_write(tmp_path, content)))
    assert len(objs) == 2
    assert objs[0]["netname"] == ["FIRST"]
    assert objs[1]["netname"] == ["SECOND"]


def test_continuation_via_space_prefix(tmp_path: Path) -> None:
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
descr:          First line
                Second line
source:         RIPE
"""
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert obj["descr"] == ["First line Second line"]


def test_continuation_via_plus_prefix(tmp_path: Path) -> None:
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
descr:          First line
+               Plus line
source:         RIPE
"""
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert obj["descr"] == ["First line Plus line"]


def test_continuation_via_tab_prefix(tmp_path: Path) -> None:
    content = "inetnum: 193.0.0.0 - 193.0.0.255\ndescr: First line\n\tTab line\nsource: RIPE\n"
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert obj["descr"] == ["First line Tab line"]


def test_continuation_after_repeated_attr(tmp_path: Path) -> None:
    """Continuation должно продолжать ПОСЛЕДНИЙ инстанс repeated attr."""
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
admin-c:        ALPHA
admin-c:        BRAVO
                continuation of bravo
source:         RIPE
"""
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert obj["admin-c"] == ["ALPHA", "BRAVO continuation of bravo"]


def test_comments_skipped(tmp_path: Path) -> None:
    content = """\
% RIPE WHOIS dump header
# generated 2026-05-18
inetnum:        193.0.0.0 - 193.0.0.255
% inline-ish comment in object
netname:        RIPE-NCC
source:         RIPE
"""
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert "%" not in obj
    assert "#" not in obj
    assert obj["inetnum"] == ["193.0.0.0 - 193.0.0.255"]
    assert obj["netname"] == ["RIPE-NCC"]


def test_empty_value_preserved(tmp_path: Path) -> None:
    """``org:`` без значения → ``obj["org"] == [""]`` (Q2)."""
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
org:
source:         RIPE
"""
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert obj["org"] == [""]


def test_repeated_attrs_collected(tmp_path: Path) -> None:
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
descr:          One
descr:          Two
descr:          Three
source:         RIPE
"""
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert obj["descr"] == ["One", "Two", "Three"]


def test_case_normalization(tmp_path: Path) -> None:
    """Mixed-case ключи нормализуются в lowercase (Q3)."""
    content = """\
Inetnum:        193.0.0.0 - 193.0.0.255
NETNAME:        RIPE-NCC
Admin-C:        BRD-RIPE
Source:         RIPE
"""
    obj = next(parse_rpsl(_write(tmp_path, content)))
    assert "inetnum" in obj
    assert "Inetnum" not in obj
    assert obj["netname"] == ["RIPE-NCC"]
    assert obj["admin-c"] == ["BRD-RIPE"]


def test_object_type_via_first_key(tmp_path: Path) -> None:
    """``next(iter(obj))`` возвращает primary attribute (Q7 contract)."""
    content = """\
aut-num:        AS3333
as-name:        RIPE-NCC-AS
source:         RIPE

organisation:   ORG-RIEN1-RIPE
org-name:       Reseaux IP Europeens
source:         RIPE
"""
    objs = list(parse_rpsl(_write(tmp_path, content)))
    assert next(iter(objs[0])) == "aut-num"
    assert next(iter(objs[1])) == "organisation"


def test_malformed_line_without_colon_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
this line has no colon and is malformed
netname:        RIPE-NCC
source:         RIPE
"""
    with caplog.at_level(logging.WARNING, logger="rir2localdb.parsers.rpsl"):
        obj = next(parse_rpsl(_write(tmp_path, content)))
    assert obj["inetnum"] == ["193.0.0.0 - 193.0.0.255"]
    assert obj["netname"] == ["RIPE-NCC"]
    assert any("malformed" in r.message.lower() for r in caplog.records)


def test_tail_object_without_trailing_newline(tmp_path: Path) -> None:
    """Последний объект без `\\n` в конце — yield'ится."""
    content = "inetnum: 193.0.0.0 - 193.0.0.255\nnetname: RIPE-NCC\nsource: RIPE"
    objs = list(parse_rpsl(_write(tmp_path, content)))
    assert len(objs) == 1
    assert objs[0]["source"] == ["RIPE"]


def test_gzip_auto_detected_by_magic_bytes(tmp_path: Path) -> None:
    content = "inetnum: 193.0.0.0 - 193.0.0.255\nnetname: RIPE-NCC\nsource: RIPE\n"
    p = tmp_path / "rpsl.gz"
    p.write_bytes(gzip.compress(content.encode("utf-8")))
    objs = list(parse_rpsl(p))
    assert len(objs) == 1
    assert objs[0]["netname"] == ["RIPE-NCC"]


def test_plain_text_works_too(tmp_path: Path) -> None:
    """Без gzip — plain text тоже корректно."""
    p = _write(tmp_path, "inetnum: 1.2.3.0 - 1.2.3.255\nsource: RIPE\n")
    objs = list(parse_rpsl(p))
    assert len(objs) == 1
    assert objs[0]["inetnum"] == ["1.2.3.0 - 1.2.3.255"]


def test_empty_file_yields_nothing(tmp_path: Path) -> None:
    objs = list(parse_rpsl(_write(tmp_path, "")))
    assert objs == []


def test_comments_only_no_object(tmp_path: Path) -> None:
    content = """\
% header line 1
# header line 2
% nothing else
"""
    objs = list(parse_rpsl(_write(tmp_path, content)))
    assert objs == []


def test_stats_counts_match(tmp_path: Path) -> None:
    content = """\
inetnum:        193.0.0.0 - 193.0.0.255
source:         RIPE

aut-num:        AS3333
source:         RIPE

organisation:   ORG-RIEN1-RIPE
source:         RIPE
"""
    p = _write(tmp_path, content)
    it, stats = parse_rpsl_with_stats(p)
    objs = list(it)
    assert len(objs) == 3
    assert stats.objects_yielded == 3
    # Каждый объект 2 строки + 2 пустых разделителя — всего ~8 строк.
    assert stats.lines_total >= 8
    assert stats.bytes_consumed > 0
