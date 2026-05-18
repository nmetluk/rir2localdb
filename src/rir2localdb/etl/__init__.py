"""rir2localdb.etl — see docs/02-architecture.md."""

from rir2localdb.etl.delegated_etl import EtlStats, apply_delegated_etl
from rir2localdb.etl.rpsl_etl import RpslEtlStats, apply_rpsl_etl

__all__ = [
    "EtlStats",
    "RpslEtlStats",
    "apply_delegated_etl",
    "apply_rpsl_etl",
]
