# Stage 3-06: Grafana dashboard + Prometheus alert rules

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:**
- `feat(deploy): Prometheus alert rules + scrape config`
- `feat(deploy): Grafana dashboard + provisioning configs`
- `feat(deploy): observability docker-compose + Alertmanager → Telegram`
- `ci: validate alert rules via promtool, dashboard JSON via jq`
- `docs: observability deployment, PromQL cookbook, alert routing`
- `docs(session-log): 03-06 Grafana + alerts`

## Что сделано

### `deploy/prometheus/alerts.yml` (4 группы, 9 rules)

| Группа | Alerts |
|---|---|
| `rir2localdb_sync` | `SyncStale` (>36h), `SyncStaleCritical` (>72h), `SyncFailed`, `SyncDurationDegrading` (>30 min) |
| `rir2localdb_data` | `TableShrinking` (<-1000 rows/h), `SourceStale` (>48h) |
| `rir2localdb_api` | `APIHighErrorRate` (>5% 5xx), `APISlowLookups` (p95 >1s) |
| `rir2localdb_rdap` | `RDAPHighMissRate` (>1 req/s 15min) |

Severity labels: `warning` для recoverable / observable, `critical`
для broken pipeline. Annotation `description:` со ссылками на
runbook-like команды (journalctl, status --json).

Все пороги обоснованы baseline'ами Stage 3-01..05:
- 36h sync stale = daily timer + ~12h grace.
- 30 min duration vs ~8.5 min incremental baseline.
- 5% error rate — стандарт production.
- 1s p95 latency — lookup'ы по индексу должны быть <100ms.
- 1 req/s RDAP miss vs ARIN limit 0.83 req/s.

`promtool check rules` локально и в CI — SUCCESS, 9 rules.

### `deploy/prometheus/prometheus.yml`

Minimal scrape config: rule_files на alerts.yml, alerting к
alertmanager:9093, scrape `api:8000/v1/metrics` каждые 30s. Comment'ы
показывают как переписать target для bare-metal deployment.

### `deploy/grafana/dashboard.json`

5 секций, ~18 panels:

1. **Sync health** — 4 stat panels (time-since-sync, status,
   duration, RDAP cache size) с color thresholds.
2. **Data volumes** — time-series row counts × 13 tables + bar gauge
   instant + stale records line chart.
3. **Source freshness** — table из 29 строк (`time() -
   source_last_fetched_timestamp / 3600`) с color-background gradient.
4. **HTTP API** — RPS by endpoint + p95 latency + status code mix.
5. **RDAP** — cache miss rate + found vs not-found split.

Variable `$datasource` — Prometheus datasource picker. JSON прошёл
`jq empty`.

### `deploy/grafana/{datasources,dashboards}.yml`

Provisioning files: Grafana при старте автоматически добавляет
Prometheus datasource и подхватывает все dashboards из mounted
volume. Anonymous Viewer access включён для quick smoke (для prod
заменить на login + restricted org).

### `deploy/alertmanager/alertmanager.yml`

Example Telegram routing:
- Один receiver `telegram-alerts` (тот же бот, отдельный канал
  «rir2localdb alerts» для разделения dev/ops нотификаций).
- `bot_token_file` mount (не env-var — безопаснее).
- `group_by: [alertname, severity]`, `repeat_interval`: 4h warning /
  1h critical.
- HTML message format со ссылками на runbook.
- `inhibit_rules`: SyncStaleCritical подавляет SyncStale (избегаем
  дублей).

### `docker-compose.observability.yml`

Addon на main compose. 3 service'а:
- `prometheus:v2.55.0` — scrape + alert eval, retention 90d.
- `grafana:11.3.0` — dashboard provisioning.
- `alertmanager:v0.27.0` — Telegram routing.

Mount'ы:
- `./deploy/prometheus/*.yml` → `/etc/prometheus/`.
- `./deploy/grafana/*.{yml,json}` → `/etc/grafana/provisioning/`.
- `./deploy/alertmanager/alertmanager.yml` → `/etc/alertmanager/`.
- `/etc/rir2localdb/telegram_bot_token` (host) → `/etc/alertmanager/
  telegram_bot_token` (container, 0600 perm).

Persistent volumes для каждого: `prometheus-data`, `grafana-data`,
`alertmanager-data`.

### CI

Новый job `validate-observability` в `ci.yml`:
- `promtool check rules deploy/prometheus/alerts.yml`.
- `jq empty deploy/grafana/dashboard.json`.

`check config` пропустили — он валидирует container-absolute paths
(`/etc/prometheus/...`), что fail'ит на CI runner'е. Запускается при
старте Prometheus container'а в production.

### Документация

`docs/07-operations.md` § «Observability deployment»:
- Quick start (token secret + chat_id substitution + compose up).
- Smoke commands.
- Alert rules table.
- Alertmanager → Telegram setup.
- PromQL cookbook (6 готовых запросов).

## Проверки

- pytest tests/ — без изменений (151 passed, 1 deselected; Stage 3-06
  не трогает Python код).
- `promtool check rules deploy/prometheus/alerts.yml` — SUCCESS.
- `jq empty deploy/grafana/dashboard.json` — clean.
- ruff/mypy clean.

## Live smoke

Не делал на сервере — это «опциональный» observability stack,
включается только когда оператор хочет full monitoring. Smoke
заменён юнит-валидацией (promtool + jq в CI). Реальный production
deployment: `docker compose -f docker-compose.yml -f docker-compose.
observability.yml up` после установки Telegram bot token.

## Что НЕ сделали

- Alertmanager автомата с rotation/escalation — пример конфига
  достаточен; production users настраивают на своё усмотрение.
- OpenTelemetry tracing — overkill для одного backend сервиса.
- Custom Grafana plugins — стандартный Prometheus datasource хватает.
- SLO/SLI через Sloth/Pyrra — overengineering.

## Что дальше

Stage 3 целиком закрыт. См. `03-99-stage-3-closed.md` для итогового
обзора. Pause или новые features по запросу.
