# CONTEXT — точка возобновления работы

> Этот файл всегда отвечает на вопрос: «что было сделано, на чём остановились,
> что делать дальше?» **Обновляется в конце каждой рабочей сессии.**
> Если открываешь репозиторий впервые после паузы — читай этот файл первым.

---

## TL;DR проекта

`rir2localdb` качает публичные FTP/HTTPS-выгрузки пяти RIR
(AFRINIC, APNIC, ARIN, LACNIC, RIPE), кладёт их в PostgreSQL,
отдаёт по REST API whois-подобную информацию об IP-адресах и ASN.

Запускается один раз в сутки (cron), внутри ходит по каталогам RIR,
скачивает только изменившиеся файлы, парсит их, идемпотентно
обновляет таблицы.

---

## Где мы сейчас

**Stage 0: Bootstrap & planning — завершён.**

Сделано в текущей сессии:

- [x] Прочитаны спецификации форматов (NRO RIR-Statistics-Exchange-Format,
  RIPE Database split files, ARIN Bulk Whois policy, LACNIC public data).
- [x] Решены архитектурные вопросы: язык (Python 3.12), БД (PostgreSQL 16),
  транспорт (HTTPS, не FTP), хранение диапазонов (`int8range` / `numrange`
  с GiST), миграции (Alembic), API (FastAPI). См. `docs/adr/`.
- [x] Создан скелет репозитория: `pyproject.toml`, `src/` layout, пустые
  пакеты с `__init__.py`, `docker-compose.yml` для локального Postgres,
  `.env.example`, `.gitignore`.
- [x] Написан каталог источников `src/rir2localdb/sources.py` — это
  **реальный** код с URL/метаданными, не заглушка. С него начинается
  Stage 1.
- [x] Написана вся документация в `docs/` (10 файлов + 5 ADR).

Не сделано (намеренно, ждёт Stage 1):

- [ ] Реализация модулей `sync/`, `parsers/`, `etl/`, `api/` —
  сейчас это пустые `__init__.py` плюс однострочные TODO-стабы.
- [ ] Миграции Alembic — каталог `migrations/` пуст, шаблон поднимается
  одной командой `alembic init migrations`.
- [ ] CI / тесты — каркас `tests/` есть, заполняется в Stage 1.

---

## Что делать дальше (Stage 1)

Подробный список — `docs/08-roadmap.md` раздел «Stage 1». Кратко:

1. `alembic init migrations` → подложить наш `alembic.ini` и `env.py`.
2. Написать первую миграцию: таблицы `sync_files`, `sync_runs`,
   `ip_allocation`, `asn_allocation` (см. `docs/03-database-schema.md`).
3. Реализовать `sync/fetcher.py` — асинхронный HTTPS-загрузчик с
   условным GET (`If-Modified-Since` + ETag) и валидацией md5.
4. Реализовать `sync/state.py` — учёт состояния каждого файла в БД.
5. Реализовать `parsers/delegated.py` — парсер пайп-формата
   (фактически 50 строк, см. `docs/05-parsers.md`).
6. Реализовать `etl/delegated_etl.py` — `COPY`-загрузка во временную
   таблицу + `INSERT ... ON CONFLICT` swap.
7. CLI-команды `rir2localdb sync --tier core` и `rir2localdb status`.
8. Минимальный API: `GET /ip/{addr}` и `GET /asn/{num}` поверх
   `ip_allocation` / `asn_allocation`.

**Definition of Done для Stage 1:** на чистой машине проходит сценарий
быстрого старта из `README.md`, `curl http://localhost:8000/ip/8.8.8.8`
возвращает корректный JSON с реестром, страной и статусом.

---

## Открытые вопросы (нужно решить до Stage 2)

1. **ARIN Bulk Whois.** Получаем ли API-ключ у ARIN? Без него по ARIN
   будут только данные из delegated stats — без названий организаций
   и POC. Решение откладывается до Stage 2, но запросить ключ имеет
   смысл заранее, согласование занимает время.
2. **LACNIC rich data.** Полного публичного дампа нет. Варианты:
   а) ограничиться delegated stats; б) подключить RDAP-обогащение
   с rate-limit. По умолчанию — (а), (б) опционально в Stage 2.
3. **Что считать «whois-ответом» в API.** Минимум — поля из delegated
   stats. Максимум — слияние с RPSL inetnum/aut-num/organisation.
   В Stage 1 ограничиваемся минимумом, в Stage 2 расширяем.

---

## Где что лежит (карта репозитория)

```
rir2localdb/
├── README.md, ROADMAP.md, CONTEXT.md   ← навигация (читать сначала)
├── docs/                               ← вся проектная документация
│   ├── 00..09-*.md                     ← разделы по темам
│   └── adr/                            ← architecture decision records
├── src/rir2localdb/
│   ├── sources.py                      ← каталог URL и форматов (готов)
│   ├── config.py, cli.py               ← заглушки
│   ├── db/, sync/, parsers/, etl/, api/  ← пустые модули, Stage 1
├── migrations/                         ← Alembic, пока пусто
├── tests/                              ← pytest, пока пусто
├── scripts/                            ← вспомогательные shell-скрипты
├── pyproject.toml                      ← деплой/зависимости
├── docker-compose.yml                  ← локальный Postgres
└── .env.example                        ← образец переменных окружения
```

---

## Контакты внешних систем (для справки)

| RIR     | Базовый URL                                | Богатый whois? |
|---------|--------------------------------------------|----------------|
| AFRINIC | https://ftp.afrinic.net/pub/               | да (`dbase/afrinic.db.gz`) |
| APNIC   | https://ftp.apnic.net/                     | да (`pub/apnic/whois/*.gz`) |
| ARIN    | https://ftp.arin.net/pub/                  | только по API-ключу |
| LACNIC  | https://ftp.lacnic.net/pub/                | нет (только delegated) |
| RIPE    | https://ftp.ripe.net/                      | да (`ripe/dbase/split/*.gz`) |

Полный машинно-читаемый каталог — `src/rir2localdb/sources.py`.

---

## Чек-лист обновления этого файла

После каждой значимой рабочей сессии:

1. Перенести готовое из «Что делать дальше» в «Где мы сейчас».
2. Дополнить «Открытые вопросы», если что-то выяснилось.
3. Если изменилась структура — обновить «Где что лежит».
4. Коммит с сообщением `chore(context): update after <stage/topic>`.
