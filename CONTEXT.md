# CONTEXT — точка возобновления работы

> Обновляется в конце каждой рабочей сессии. Если открываешь репозиторий
> впервые после паузы — читай этот файл первым.

---

## TL;DR проекта

`rir2localdb` качает публичные HTTPS-выгрузки пяти RIR
(AFRINIC, APNIC, ARIN, LACNIC, RIPE), кладёт их в PostgreSQL,
отдаёт по REST API whois-подобную информацию об IP-адресах и ASN.

Запускается раз в сутки (cron), внутри ходит по каталогам RIR,
скачивает только изменившиеся файлы, парсит их, идемпотентно обновляет
таблицы.

---

## Где мы сейчас

- **Stage 0** ✅ Bootstrap & planning.
- **Stage 1** ✅ Core sync + minimal API (2026-05-18). См.
  `.claude/session-log/01-99-stage-1-closed.md`.
- **Stage 1.50** ✅ Стабилизация (2026-05-18). `01-50-stabilization.md`.
- **Stage 2** ✅ RPSL rich-tier — 8 типов объектов, API enrichment
  (2026-05-18). `02-99-stage-2-closed.md`.
- **Stage 2.50** ✅ RPSL coverage до 11 типов + `rir` normalization
  + CONTEXT cleanup (2026-05-18). `02-50-rpsl-completeness-and-cleanup.md`.
- **Stage 3** ✅ ЗАКРЫТ ЦЕЛИКОМ (2026-05-18). Operations cycle:
  - **3-01** ✅ systemd timer + service. `03-01-systemd-timer.md`.
  - **3-02** ✅ Prometheus `/v1/metrics` + structured JSON logs
    + `clock_timestamp()` fix для finished_at.
    `03-02-metrics-and-structured-logs.md`.
  - **3-03** ✅ Stale-records GC, `is_stale` + 7-run grace. ADR-0008.
    `03-03-stale-records-gc.md`.
  - **3-04** ✅ Dockerfile + compose. Multi-stage, один image / три CMD.
    `03-04-docker.md`.
  - **3-05** ✅ RDAP fallback для ARIN. ADR-0009. Known limitation:
    APNIC catch-all IANA-NETBLOCK маскирует bulk-miss → refine'м в
    follow-up. `03-05-rdap-fallback.md`.
  - **3-06** ✅ Grafana + Prometheus alerts. 4 группы alerts,
    5-section dashboard, Alertmanager→Telegram example.
    `03-06-grafana-and-alerts.md`.

  Итоги Stage 3 — `.claude/session-log/03-99-stage-3-closed.md`.
  - 3-05 ⏳ RDAP fallback (опц.).
  - 3-06 ⏳ Grafana + alerts.
  - 3-07 ⏳ API mntner/person/as_set (опц.).

## Текущие цифры

- ~39 модулей в `src/rir2localdb/`.
- 151 unit-тест + 1 integration smoke (`pytest -m integration`).
- 9 ADR.
- 6 миграций (0001-0006).
- 13 RPSL таблиц + 2 delegated + rdap_cache.
- ~120 коммитов после bootstrap.
- Live state: 1 full sync (24 мин) + 2 incremental (8.5 мин + 4.6 sec),
  9.65M RPSL objects + 759k delegated.

## Что дальше

Pause. Все обещанные функции есть, infra для prod готова,
observability есть. Утилита по-настоящему готова к long-running
deployment.

Опциональные follow-ups (по запросу):

1. **IANA-NETBLOCK suppression** для RDAP fallback (Stage 3-05
   known limitation).
2. **LACNIC RDAP-fallback** — отдельный module по образцу ARIN.
3. **ARIN Bulk Whois API** — env-gated, для тех, кто готов пройти ToU.
4. **API расширение под mntner/person/as_set** — данные в БД,
   REST endpoint'ов нет.
5. **GH Container Registry push** — Docker image сейчас build'ится
   ephemerally в CI; реальный push требует credentials.
6. **Kubernetes Helm chart** / **distroless image** / **BGP-таблицы
   RIPE RIS** / **partitioning при >50M строк** — Stage 4+ если будет
   запрос.

## Где что лежит

```
src/rir2localdb/
├── sources.py                  каталог URL + Rir/Tier/Format enums
├── config.py, cli.py           Settings + Typer CLI (5 команд)
├── logging_setup.py            plain-text logging (Stage 3 заменит JSON)
├── db/{engine,models}.py
├── migrations/versions/
│   ├── 0001_initial_schema.py      ← Stage 1
│   ├── 0002_rpsl_tables.py         ← Stage 2-02 (8 RPSL таблиц)
│   ├── 0003_more_rpsl_tables.py    ← Stage 2.50 § A (mntner/person/as_set)
│   └── 0004_normalize_rir_ripe_to_ripencc.py  ← Stage 2.50 § B
├── sync/{fetcher,state,orchestrator}.py
├── parsers/{delegated,rpsl}.py
├── etl/{delegated_etl,rpsl_etl}.py
└── api/
    ├── app.py, schemas.py
    └── routers/{ip,asn,meta}.py

tests/
├── test_{fetcher,state,delegated_parser,delegated_etl}.py
├── test_rpsl_{parser,migration,etl}.py
├── test_orchestrator.py, test_api_smoke.py, test_cli_status.py
└── integration/test_live_ripe.py

.claude/
├── WORKFLOW.md                 cooperative Claude workflow (ADR-0006)
└── session-log/                по одному файлу на шаг
```

## Контакты внешних систем

| RIR     | Базовый URL                                | rich whois? |
|---------|--------------------------------------------|-------------|
| AFRINIC | https://ftp.afrinic.net/pub/               | да (`dbase/afrinic.db.gz`) |
| APNIC   | https://ftp.apnic.net/                     | да (`pub/apnic/whois/*.gz`) |
| ARIN    | https://ftp.arin.net/pub/                  | только IRR + опц. Bulk Whois ключ |
| LACNIC  | https://ftp.lacnic.net/pub/                | нет (только delegated) |
| RIPE    | https://ftp.ripe.net/                      | да (`ripe/dbase/split/*.gz`) |

Каталог в коде: `src/rir2localdb/sources.py`.

---

## Чек-лист обновления этого файла

После каждой значимой рабочей сессии:

1. Передвинуть пункт «Где мы сейчас», если закрылся stage / followup.
2. Дополнить «Открытые вопросы», если что-то выяснилось.
3. Если изменилась структура — обновить «Где что лежит».
4. Коммит с сообщением `chore(context): update after <stage/topic>`.

Исторические детали по шагам — в `.claude/session-log/*.md`. Сюда не
дублируем.
