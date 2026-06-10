# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

См. также `AGENTS.md` — там те же сведения для Codex-workflow (откуда берутся
провайдеры/ключи, какой конфиг править).

## Назначение

Бенчмарк автономных кодинг-агентов. На один прогон поднимается N параллельных
копий `opencode serve` (каждая — свой порт, своя рабочая папка), всем даётся одна
задача, результаты (статус, время, токены, цена, артефакты) пишутся в
`data/main.db`. База — **единственный источник правды**; JSON-файлов с данными на
диске нет, и она **коммитится в git** (в CI данные взять больше неоткуда).

## Команды

```bash
# Прогон бенчмарка (--project обязателен; по умолчанию -n 5 копий)
python bench.py --project hello_world "напиши hello world на питоне"
python bench.py --project my_task -p zai-coding-plan -m glm-5.1 -n 3 "..."
python bench.py --project my_task -f task.txt           # задача из файла
python bench.py --project my_task --timeout 0 "..."     # 0 = без лимита времени

# Локальный дашборд (пересобирает docs/data/index.json из базы на лету)
python bench.py serve --port 8000

# Проверка доступности моделей (поднимает ОДИН serve, гоняет модели последовательно)
python check_models.py --provider opencode
python check_models.py --models zai-coding-plan/glm-5.1 --pay-models

# Сборка статического индекса для GitHub Pages
python scripts/build_index.py

# Управление denylist-ом моделей
python scripts/model_exclusions.py list
python scripts/model_exclusions.py block <provider>/<model> --reason "..."

# Экспорт сохранённых артефактов прогонов из базы
python scripts/run_artifacts.py ...

# Проверки
python -m pytest                          # весь набор (testpaths=tests)
python -m pytest tests/test_bench.py::BenchCriticalBugTests::<имя_метода>  # один тест
ruff check .                              # линт (дефолтные правила, конфиг-файла нет)
python -m py_compile bench.py             # быстрая проверка синтаксиса
```

Тесты — `unittest.TestCase`, запускаются через pytest. Сетевые/`opencode`-вызовы
замоканы (`FakeHttpClient`, `FakeProcess` в `tests/test_bench.py`), так что прогон
не требует ни сервера, ни ключей.

## Архитектура

Поток одного прогона (`run_benchmark` в `benchmark_report.py`):

1. `load_project` тянет задание из таблицы `projects_library` (или берёт из CLI/`-f`).
2. `ensure_model_is_allowed` — fail-fast, если пара provider/model в активном
   denylist-е (`model_exclusions`); обходится через `--force-excluded`.
3. `prepare_work_dirs` создаёт по папке на копию: `data/result/<project>/<provider>_<model>/<timestamp>_<N>/`.
4. `ThreadPoolExecutor` запускает копии параллельно + одну фоновую задачу на цену
   (`get_pricing`). Каждая копия (`run_copy`) поднимает свой `opencode serve` и
   гоняет сессию через `probe_session`.
5. Цены и токены сводятся, отчёт целиком пишется в базу через `save_report` →
   `upsert_report`, артефакты собираются и кладутся туда же, рабочие папки на диске
   зачищаются (`cleanup_collected_artifacts`).

Слои (каждый — отдельная ответственность, читать вместе):

- `bench.py` — тонкий CLI: парсинг аргументов, валидация, установка
  shutdown-хендлеров; делегирует в `run_benchmark`/`serve`. Подкоманда `serve`
  ветвится по `sys.argv[1] == "serve"` до основного парсера.
- `opencode_runtime.py` — всё про `opencode serve`: запуск процессов и их учёт
  (`_server_processes`/`_server_owners` под `_server_lock`), `atexit`+сигнальное
  гашение, SSE-чтение событий сессии, вытаскивание реальной причины ошибки из
  файлового лога opencode (`_opencode_error_tail`), пути рабочих папок, дефолты
  (`DEFAULT_MODEL`, `DEFAULT_PROVIDER`, `DEFAULT_BASE_PORT` и т.д.).
- `benchmark_report.py` — оркестрация, печать сводок, запись отчёта в базу.
- `db.py` — SQLite-схема (`SCHEMA`) и весь DB API. `connect()` включает WAL +
  `busy_timeout=5000` + foreign keys — это сознательно, чтобы параллельные
  bench.py/check_models/pricing не ловили «database is locked».
- `pricing.py` — каталог цен поверх OpenRouter SDK (публичный `/models`, кэш в
  таблицах `openrouter_cache*`, TTL 24ч) + ручные цены из `price_overrides`/
  `price_aliases`/`provider_notes`. `get_pricing(refresh=False)` ходит только в
  кэш базы — это режим детерминированной сборки индекса.
- `usage.py` — извлечение токенов из ответов сессии и локальная оценка стоимости.
- `artifacts.py` — сбор файлов-результатов агента и логов, дедупликация по sha256.
- `index_builder.py` — собирает `docs/data/index.json` из базы (с рейтингом
  моделей и группировкой по проектам); активный denylist отсекается прямо в SQL.
- `dashboard_server.py` — локальный статический сервер `docs/`, пересобирает
  индекс при изменении mtime базы (по `_db_fingerprint`).
- `check_models.py` — отдельный диагностический CLI (доступность моделей), делит
  runtime с бенчмарком.
- `scripts/` — CLI-обёртки и утилиты: `build_index.py` (для CI Pages),
  `run_artifacts.py` (экспорт), `model_exclusions.py` (denylist).

## Конфигурация и инварианты

- `opencode.json` (проектный) — **только описание агента** `bench_coder`: системный
  промпт + разрешения (`webfetch`/`websearch`/`external_directory` запрещены —
  агент работает офлайн в своей папке). Провайдеры и API-ключи берутся из
  **глобального** opencode-конфига (`~/.config/opencode/`,
  `~/.local/share/opencode/auth.json`) — правь их там, не здесь.
- Отчёты идемпотентны по ключу `(project, provider, model, started_at)` —
  повторный `upsert_report` обновляет строку, `runs` перезаписываются целиком.
- `raw_json` в `reports`/`projects_library` хранит дословный JSON: пересборка
  индекса должна быть байт-в-байт воспроизводимой, иначе ломается фронтенд.
- Цена в отчёте — **рыночная по платному аналогу** модели в каталоге OpenRouter,
  независимо от того, через какой (возможно бесплатный/подписочный) провайдер шёл
  тест. `:free`-варианты при разрешении id проигрывают платным (см.
  `_resolve_catalog_id`).
- `data/main.db-wal`/`-shm` — эфемерны, в `.gitignore`. Папки `data/result/*` и
  `docs/data/index.json` тоже игнорируются (артефакты живут в базе, индекс
  собирается на лету).
- CI (`.github/workflows/pages.yml`) на push в `main` (при изменении базы/`docs`/
  билдеров) пересобирает индекс и деплоит `docs/` на GitHub Pages.

## Стиль кода

Python 3.12+, type hints, PEP 8. Сообщения/комментарии в проекте — на русском.
