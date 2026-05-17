"""HTTPS fetcher with conditional GET and md5 validation.

Public API:
    async def fetch(source: Source, *, http: httpx.AsyncClient,
                    state: SyncFileState) -> FetchResult

Behavior described in docs/04-sync-pipeline.md (three-tier change
detection: md5 sidecar → If-Modified-Since/ETag → content sha256).
"""
# TODO(stage-1)
