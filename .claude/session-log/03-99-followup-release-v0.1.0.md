# Followup: v0.1.0 release tag

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:** `docs: CHANGELOG.md for v0.1.0 release` (`915aa09`)
**Tag:** `v0.1.0` → `915aa09`

## Что сделано

- **`CHANGELOG.md`** в корне репо. Формат Keep a Changelog 1.1.0,
  SemVer. Один [0.1.0] раздел с per-stage breakdown (1 / 1.50 / 2 /
  2.50 / 3), migrations list, ADR pointer, known limitations.
- **Commit** `915aa09` в main.
- **Annotated tag `v0.1.0`** через `git tag -a` с full release notes
  в tag-сообщении (видны через `git show v0.1.0`).
- **Push** обоих: `main` и `v0.1.0` в `origin`.

## Финальное состояние репо

```
$ git status
On branch main
Your branch is up to date with 'origin/main'.
nothing to commit, working tree clean

$ git tag -l
v0.1.0

$ git branch -a
* main
  remotes/origin/main

$ git log -n 3 --oneline
915aa09 docs: CHANGELOG.md for v0.1.0 release
ac858c0 chore(context): stage 3 closed
473e991 docs(session-log): 03-06 + 03-99 stage 3 closed
```

## Цифры

- **168 коммитов** на main (включая bootstrap).
- **31 session-log** файл (`.claude/session-log/*.md`).
- **9 ADR** (`docs/adr/0001..0009`).
- **6 миграций** (`src/rir2localdb/migrations/versions/0001..0006`).
- **151 unit-тест** + 1 integration smoke (`pytest -m integration`).
- **0 uncommitted changes**, **0 untracked files**.
- **0 feature-веток** — единственная ветка `main`.

## Где репо

- GitHub: https://github.com/nmetluk/rir2localdb
- `origin/main` HEAD = `915aa09` = `v0.1.0` = production-ready snapshot.
- Tag page на GitHub автоматически создаст черновую страницу release;
  планировщик передаст текст release notes для копирования в UI.

## Что НЕ делали

- Push в Docker registry — отдельная задача после релиза.
- GitHub Release UI page заполнить — это вручную через UI.
- Бамп версии — для будущего minor/patch release создаётся новый
  раздел в CHANGELOG поверх `[0.1.0]`.

## Что дальше

Пауза. Если кто-то нашёл баг или захотел follow-up из списка
«known limitations» — отдельный PR / шаг.
