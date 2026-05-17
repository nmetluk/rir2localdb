"""ETL: DelegatedRecord stream -> ip_allocation / asn_allocation.

Uses asyncpg copy_records_to_table into a staging temp table,
then INSERT ... ON CONFLICT swap. See docs/03-database-schema.md.
"""
# TODO(stage-1)
