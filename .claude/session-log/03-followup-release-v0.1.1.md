# Followup: v0.1.1 release tag

**Дата:** 2026-05-19
**Статус:** ✅ закрыт
**Коммиты:** `2f27052 chore(release): bump version to 0.1.1`,
`6a1343b docs(changelog): finalize 0.1.1 release notes`
**Tag:** `v0.1.1` → `6a1343b`

## Что сделано

- **Version bump** `0.1.0` → `0.1.1` в `src/rir2localdb/__init__.py`
  и `pyproject.toml`. Один коммит `2f27052`.
- **CHANGELOG.md finalized.** Раздел `[Unreleased]` промотирован в
  `[0.1.1] — 2026-05-19`. Reference-link `[0.1.1]: ...` добавлен в
  footer перед `[0.1.0]: ...`. Коммит `6a1343b`.
- **Annotated tag `v0.1.1`** на `6a1343b` (CHANGELOG-коммит, не
  session-log) через `git tag -a` с full release notes в
  tag-сообщении.
- **Push** `main` и `v0.1.1` в `origin`.

## Что включает v0.1.1

Первый release после feedback'а от реального пользователя
(whois-watcher integration). Закрывает gap bare-metal long-running
API deployment'а через hardened systemd-юнит
`rir2localdb-serve.service` с параметризуемым bind через
`/etc/rir2localdb/serve.env`. Плюс критический fix `log_config=None`
в `uvicorn.run()` чтобы structlog setup не перебивался при старте
uvicorn'а в systemd-контексте (без fix'а uvicorn-логи в production
были plain-text, application-логи — JSON, ломая jq-парсинг в
journald). Документация workflow обновлена с «Server tasks safety
rules» в `.claude/WORKFLOW.md` после tmux-suicide incident'а во время
production deployment'а (см. `03-followup-serve-systemd.md` §
«Lessons learned»).

## Что НЕ включает

- Никаких изменений в `sync.service` или sync-pipeline'е.
- Никаких изменений в API surface (`/v1/...` endpoints idem).
- Никаких миграций схемы (последняя — 0006 в v0.1.0).
- Stage 3-06 (Grafana dashboard + alert rules + Alertmanager →
  Telegram) уже был в v0.1.0 — не upgrade-блокер.

## Финальное состояние репо

```
$ git status
On branch main
Your branch is up to date with 'origin/main'.
nothing to commit, working tree clean

$ git tag -l
v0.1.0
v0.1.1

$ git log -n 5 --oneline
<session-log-commit> docs(session-log): 03-followup release v0.1.1
6a1343b docs(changelog): finalize 0.1.1 release notes      ← v0.1.1
2f27052 chore(release): bump version to 0.1.1
9a87c93 docs: tmux suicide lessons learned + WORKFLOW server safety rules
12a224c docs(session-log): 03-followup serve.service
```

## Что дальше

Pause. Open follow-ups (зафиксированы в v0.1.0 changelog «Known
limitations» + `03-99-stage-3-closed.md`):

- **IANA-NETBLOCK fix** — RDAP fallback для ARIN не активируется
  когда APNIC bulk RPSL содержит catch-all `IANA-NETBLOCK-X`
  placeholder'ы для ARIN-блоков.
- **LACNIC RDAP fallback** — LACNIC не публикует public WHOIS dump;
  только delegated stats. RDAP fallback не реализован.
- **ARIN Bulk Whois API** — требует ARIN API key + ToU acceptance.
- **mntner / person API** — таблицы загружены, но не expose'нуты
  через `/v1/...` endpoints (см. open вопросы 03-99).

Любой из них — отдельный followup или Stage 4 по запросу.
