"""Parser for the NRO delegated-extended pipe format.

See docs/05-parsers.md for the format spec and edge cases.

Public API:
    @dataclass class DelegatedRecord: ...
    def parse(path: Path) -> Iterator[DelegatedRecord]
"""
# TODO(stage-1)
