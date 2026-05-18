# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-05-19

First release driven by real-world feedback (whois-watcher integration).
Bare-metal long-running API server deployment was a gap in v0.1.0 —
operators had to choose between unmanaged tmux processes or Docker compose.
v0.1.1 closes this with a hardened systemd service unit, plus a critical
fix for structured logs in systemd context.

### Added
- `deploy/systemd/rir2localdb-serve.service` — systemd unit for long-running
  FastAPI server. Parameterized bind via `/etc/rir2localdb/serve.env`
  (HOST/PORT env vars), so the same unit serves all deployment topologies
  (`127.0.0.1` for standalone, private bridge gateway for Docker-network
  integration, `0.0.0.0` for public exposure with external firewall).
- `install-systemd.sh` now installs and enables (but does not autostart)
  `serve.service`. Operator decides when to launch.
- Hardening identical to `sync.service` minus `ReadWritePaths` (serve does
  not write to disk).
- `AF_NETLINK` in `RestrictAddressFamilies` for async DNS resolution in
  asyncpg/httpx.
- Documentation: `docs/07-operations.md` § "HTTP API server via systemd"
  with override examples and rationale for private bridge bind vs `0.0.0.0`
  + firewall.

### Fixed
- `rir2localdb serve` passes `log_config=None` to uvicorn. Without this,
  uvicorn installs its own root-logger dictConfig at startup, overriding
  the structlog handler from `configure_logging()`. Result: in
  production-mode systemd run with `RIR2LOCALDB_LOG_FORMAT=json`,
  uvicorn-emitted logs (request/access/error) were plain-text while
  application logs were JSON — broken jq parsing in journald.

### Documentation
- `WORKFLOW.md` § "Server tasks safety rules" — guidelines for Claude Code
  operating on production servers (unique tmux session names, no
  blanket-kill of own session, prefer systemd over tmux for daemons).
- Session-log `03-followup-serve-systemd.md` includes "Lessons learned"
  section documenting a tmux suicide incident during initial deployment.

### Operations
- First production deployment verified: serve.service active on
  `172.28.0.1:18000` (Docker bridge gateway for whois-watcher integration),
  sync.service successful (sync_run id=7), sync.timer active waiting for
  next 03:00 UTC trigger.

## [0.1.0] — 2026-05-18

Initial public release. Functionally complete end-to-end pipeline from RIR
data sources to whois-style REST API, with production-ready ops layer.

### Highlights

- **Coverage:** all 5 RIRs (AFRINIC, APNIC, ARIN, LACNIC, RIPE NCC), 29 source URLs.
- **Data types:** NRO delegated stats + 11 RPSL object types
  (inetnum, inet6num, aut-num, organisation, role, route, route6,
  as-block, mntner, person, as-set) + ARIN IRR (route/route6/as-set/mntner).
- **API:** `/v1/ip/{addr}` and `/v1/asn/{num}` with RPSL enrichment,
  `/v1/status`, `/v1/healthz`, `/v1/readyz`, `/v1/metrics`.
- **Sync lifecycle:** atomic transaction (ETL → success → GC → finalize stats),
  3-tier change detection (md5 sidecar → conditional GET → SHA256 of body).
- **Deployment:** systemd (bare-metal, hardened sandbox) or Docker (multi-stage,
  non-root, compose stack).
- **Observability:** Prometheus exposition + structured JSON logs (structlog) +
  Grafana dashboard + 9 alert rules + Alertmanager → Telegram example.
- **Data hygiene:** soft-delete via `is_stale` + 7-run grace period;
  API hides stale by default with opt-in via `?include_stale=true`.
- **Optional RDAP fallback** for ARIN ownership/contacts (opt-in via env).

### Stage 1 — Core sync + minimal API

- HTTPS fetcher with 3-tier change detection.
- NRO delegated parser (5 sources).
- ETL with batched COPY + UPSERT.
- FastAPI endpoints `/v1/ip`, `/v1/asn`, `/v1/status`, `/v1/healthz`.
- Initial schema migrations.
- Live DoD: 646k IP + 113k ASN allocations loaded in ~102 seconds.

### Stage 1.50 — Stabilization

- CI workflow with PostgreSQL service, ruff, mypy, alembic round-trip, pytest.
- Wheel-packaged migrations via `importlib.resources`.
- `/v1/readyz` split from `/v1/healthz`.
- Per-RIR summary in `/v1/status`.
- `prefix_length` for CIDR-aligned IPv4 ETL records.

### Stage 2 — RPSL rich-tier

- Streaming gzip RPSL parser (continuation lines, repeated attributes, comments).
- 8 RPSL tables (one per object type) with GiST partial indexes.
- Batched ETL with per-table staging + JSONB binary codec for COPY.
- API enrichment (`rpsl` block with inetnum + organisation + aut_num).
- Format-based orchestrator dispatch (DELEGATED ↔ RPSL).
- ARIN IRR support as part of rich tier.
- Live DoD: 9.65M RPSL objects loaded in ~24 minutes;
  `/v1/ip/193.0.6.139` returns `rpsl.inetnum.netname="RIPE-NCC"` and
  `rpsl.organisation.org_name="Reseaux IP Europeens Network Coordination Centre"`.

### Stage 2.50 — RPSL completeness + tech-debt cleanup

- 3 additional RPSL tables (mntner, person, as_set) — full coverage of 11 types.
- `rir` value canonicalized to NRO names (`ripencc` instead of `ripe`).
- ARIN zero-padded IPv4 prefix normalization (`069.031.132.000/23` → `69.31.132.0/23`).
- CLI `status --json` for machine-readable monitoring output.
- CONTEXT.md rewrite for compactness.

### Stage 3 — Production ops

- **systemd timer + service** with hardened sandbox (ProtectHome, RestrictAddressFamilies, etc.).
- **Prometheus `/v1/metrics`** with sync/data/source/HTTP/RDAP gauges and counters.
- **Structured JSON logs** via structlog (`RIR2LOCALDB_LOG_FORMAT=json`).
- **Stale-records GC** with `is_stale` soft-delete + 7-run grace period.
- **Docker image + compose** (multi-stage, python:3.12-slim, non-root, ~335MB).
- **RDAP fallback for ARIN** ownership (opt-in, transparent, DB-cached).
- **Grafana dashboard + Prometheus alert rules** (9 rules across 4 groups).
- **Alertmanager → Telegram** example configuration.

### Migrations

- 0001 — initial schema (sync_run, sync_file, ip_allocation, asn_allocation).
- 0002 — 8 RPSL tables (inetnum/inet6num/aut_num/organisation/role/route/route6/as_block).
- 0003 — 3 more RPSL tables (mntner/person/as_set).
- 0004 — normalize `rir` from `"ripe"` to `"ripencc"`.
- 0005 — `is_stale` columns + active partial GiST indexes.
- 0006 — `rdap_cache` table.

### Architecture decisions

ADR-0001 .. ADR-0009 covering: Python, PostgreSQL, range storage, HTTPS not FTP,
async SQLAlchemy, cooperative Claude workflow, RPSL table layout, soft-delete
via is_stale, RDAP fallback.

### Known limitations

- **`person` table** is largely empty for RIPE-sourced data due to
  RIPE GDPR-driven PII dummification (`DUMY-RIPE` handles).
  This is upstream behaviour, not a bug.
- **RDAP fallback** for ARIN does not activate when APNIC bulk RPSL
  contains catch-all `IANA-NETBLOCK-X` placeholder objects for ARIN
  blocks. Tracked as follow-up.
- **LACNIC** does not publish a complete public WHOIS dump; only
  delegated stats are loaded. RDAP fallback for LACNIC is not yet
  implemented.
- **ARIN Bulk Whois API** integration is not implemented; project
  catalog declares the tier but ingestion requires an ARIN API key
  + ToU acceptance.

[0.1.1]: https://github.com/nmetluk/rir2localdb/releases/tag/v0.1.1
[0.1.0]: https://github.com/nmetluk/rir2localdb/releases/tag/v0.1.0
