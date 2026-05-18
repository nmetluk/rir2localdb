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
- **Stage 3** в работе.
  - **3-01** ✅ systemd timer + service (daily sync 03:00 UTC,
    hardened sandbox, live install на сервере, 2026-05-18).
    `03-01-systemd-timer.md`.
  - **3-02** ✅ Prometheus `/v1/metrics` + structured JSON logs
    через structlog (2026-05-18). `03-02-metrics-and-structured-logs.md`.
    Заодно поймал и пофиксил `now()` → `clock_timestamp()` для
    sync_run.finished_at.
  - 3-03 ⏳ Stale-records GC.
  - 3-04 ⏳ Dockerfile + compose.
  - 3-05 ⏳ RDAP fallback (опц.).
  - 3-06 ⏳ Grafana + alerts.
  - 3-07 ⏳ API mntner/person/as_set (опц.).

## Текущие цифры

- ~36 модулей в `src/rir2localdb/`.
- ~132 unit-тестов + 1 integration smoke (`pytest -m integration`).
- 7 ADR.
- 11 RPSL таблиц + 2 delegated (`ip_allocation` / `asn_allocation`).
- Last live full sync: 646k IP + 113k ASN + 9.65M RPSL objects за
  ~24 минуты.

## Что дальше — Stage 3 ops (high-level)

- **3-01** systemd timer + сервис (daily sync).
- **3-02** Prometheus `/metrics` endpoint + structured JSON logs.
- **3-03** Stale-records GC (ADR-0001).
- **3-04** Dockerfile + compose для prod (api + sync worker).
- **3-05** (опц.) RDAP fallback для ARIN ownership (бывший 2-06).
- **3-06** (опц.) Grafana dashboard + Prometheus alert rules.
- **3-07** (опц.) API расширение под `mntner` / `person` / `as_set`
  (данные уже в БД после 2.50, REST endpoint'ы — Stage 3 если решим).

Детали — `docs/08-roadmap.md` § Stage 3 (расширим перед стартом).

## Открытые вопросы

1. **LACNIC RDAP-fallback.** LACNIC не публикует полного дампа.
   Опционально подключить RDAP с rate-limit. Stage 3+.
2. **`data_dir` validation.** `run_sync` создаёт каталог через
   `mkdir(parents=True, exist_ok=True)` — опечатка в `.env` даст
   каталог в неожиданном месте. Stage 3 ops может добавить sanity-check.
3. **ARIN Bulk Whois ключ.** Опциональный fallback для полного whois
   ARIN. Решение откладывается до конкретной потребности.
4. **API расширение под mntner/person/as_set.** Stage 2.50 § A
   загрузила данные в БД, но REST endpoint'ы не добавлены. Если
   реальный спрос появится — Stage 3-07.

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
