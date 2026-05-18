"""rir2localdb.parsers — см. docs/02-architecture.md, docs/05-parsers.md."""

from rir2localdb.parsers.delegated import DelegatedRecord, parse_delegated
from rir2localdb.parsers.rpsl import (
    RpslObject,
    RpslParseStats,
    parse_rpsl,
    parse_rpsl_with_stats,
)

__all__ = [
    "DelegatedRecord",
    "RpslObject",
    "RpslParseStats",
    "parse_delegated",
    "parse_rpsl",
    "parse_rpsl_with_stats",
]
