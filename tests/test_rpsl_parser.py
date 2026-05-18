"""Тесты RPSL parser'а — stub-файл для шага 2-01a.

Реальные сценарии добавляются в 02-01b после согласования Q1-Q7
и реализации тела ``parse_rpsl``. На skeleton-этапе один skip,
чтобы pytest видел файл и было куда добавлять.
"""

from __future__ import annotations

import pytest

from rir2localdb.parsers import parse_rpsl


def test_parse_rpsl_skeleton_placeholder() -> None:
    """Placeholder. Заменяется реальными сценариями в 02-01b."""
    _ = parse_rpsl  # импортируется, контракт стабильный
    pytest.skip("parse_rpsl body lands in step 2-01b")
