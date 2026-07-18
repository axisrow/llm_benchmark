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
python bench.py --project my_task -n 1 --no-save "..."  # тестовый прогон: БД не трогаем

# Планирование: сбор уточняющих вопросов агента и автоответы (фаза plan, затем build)
python bench.py --project my_task --planning on "..."
python bench.py --project my_task --planning on --question-responder recommended "..."
python bench.py --project my_task --planning on --questions-only "..."  # только собрать вопросы

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

Тесты — `unittest.TestCase`, запускаются через pytest (`tests/test_bench.py` —
основной набор, `tests/test_artifacts.py` — сбор/дедупликация артефактов). Сетевые/
`opencode`-вызовы замоканы (`FakeHttpClient`, `FakeProcess` в `tests/test_bench.py`),
так что прогон не требует ни сервера, ни ключей. E2E-тесты дашборда
(`tests/test_e2e_dashboard.py`) гоняются через Playwright и нужны только при
правке фронтенда; smoke-тесты метрики на настоящий Ruff (`RealRuffIntegrationTests`)
skip'аются без установленного `ruff`. Зависимости тестов/E2E —
`requirements-dev.txt` (`pip install -r requirements-dev.txt`).

## Планирование и автоответы на вопросы

Режим `--planning on` (issue с фазой plan→build): перед реализацией агент может
задавать уточняющие вопросы — bench их перехватывает (`planning_questions.py`:
`capture_question_request`, нормализация, `QuestionProtocolError`) и отвечает
детерминированно по стратегии `--question-responder` (`task-text` — «ответ уже в
задании» / `recommended` / `first`). Собранные вопросы и оценки их качества
пишутся в таблицу `question_reviews` и подмешиваются в индекс (`index_builder`
строит по ним `review_key` для API). `--questions-only` — только собрать, не
отвечать и не строить план.

## Оценка Snake-реализаций через snakebench

Этот проект — **генератор**: LLM пишет Snake-реализацию (FastAPI-сервер), код
складывается в БД и на диск. Оценивает их отдельный пакет
[`snakebench`](https://github.com/axisrow/snakebench) — **он только оценивает,
ничего не генерирует**. Граница: llm_benchmark сам решает, какую папку передать;
snakebench рекурсивно находит в ней реализации и скорит по инвариантам.

Установка (пока без PyPI): `pip install -e /path/to/snakebench` или
`pip install "git+https://github.com/axisrow/snakebench.git"`.

Контракт интеграции — дать `run_suite` корень с `.py`-реализациями:

```python
import asyncio
from pathlib import Path
from snakebench import run_suite, RunConfig

reports = asyncio.run(run_suite(Path("data/result/<project>"),
                                RunConfig(n_games=200, out_dir=Path("snakebench_reports"))))
for rep in reports:
    print(rep.name, rep.final_score, rep.start_failed, rep.error)
```

snakebench находит реализации **рекурсивно**: любая листовая папка, чей `.py`
определяет module-level `app`, — одна реализация (имя = путь относительно корня,
напр. `<provider>_<model>/<stamp>_<N>`). Лишние файлы (`run.log`, `test_*.py`)
игнорируются — то есть наша раскладка `data/result/{project}/{provider}_{model}/{stamp}_{N}/`
скармливается как есть. Подробности API — в `USAGE.md` пакета snakebench.

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
- `opencode_runtime.py` — **фасад** всего, что про `opencode serve`. Начиная с
  issue #53 монолит расщеплён на листовые модули без циклов; потребители
  по-прежнему импортируют `opencode_runtime` (он ре-экспортирует публичные имена),
  но реализация живёт в отдельных файлах:
  - `opencode_base.py` — листовой (только stdlib/usage/opencode_ai): тип результата
    сессии, `Writer`, соединение с opencode, константы-настройки
    (`POST_MESSAGE_READ_TIMEOUT` и т.д.).
  - `opencode_process.py` — подъём/учёт/гашение serve-процессов; module-level
    состояние (`_server_processes`/`_server_owners` под `_server_lock`) и
    `atexit`+сигнальная регистрация живут **целиком здесь** — не разрывать.
    Подъём serve РЕТРАИТСЯ (#150, `SERVER_START_ATTEMPTS`): при одновременном
    старте нескольких `bench.py` opencode serve падает сам (`ServeError`, exit 1
    за ~3с) — конкуренция за ресурсы. Неудачная попытка ВСЕГДА гасит свой
    процесс (`stop_server`), в т.ч. когда он жив, но не ответил за таймаут —
    иначе остаётся осиротевший serve, держащий порт. ИДЕНТИЧНОСТЬ serve
    сверяется через повторный `proc.poll()` (#152): `_try_connect` отвечает лишь
    «на порту кто-то есть», а в окне между `poll()` в начале итерации и
    `_try_connect` (пауза `SERVER_CHECK_INTERVAL`) наш proc мог умереть, а чужой
    serve — занять порт. После успеха `_try_connect` перепроверяем `poll()`:
    proc жив → ИМЕННО ОН слушает порт (чужак физически не может занять сокет, пока
    наш serve-процесс жив) → успех; proc мёртв → отвечал чужой → гасим попытку.
    НЕ через `GET /app`: на opencode 1.17.18 `/app` отдаёт HTML, не JSON, и SDK
    возвращает строку — cwd оттуда не прочитать (вскрыто локальным ревью PR #153).
  - `opencode_session.py` — SSE-чтение событий сессии, отправка задачи, ожидание
    исхода (idle/error/limit/timeout), ретрай при лимите провайдера (`probe_session`).
  - `opencode_errors.py` — классификация ошибок провайдера (лимиты/аккаунт),
    скрабинг секретов/PII для публичного отчёта, чтение «хвоста» ошибок из логов
    opencode (`_opencode_error_tail`).
  - дефолты (`DEFAULT_MODEL`, `DEFAULT_PROVIDER`, `DEFAULT_BASE_PORT`, …) и пути
    рабочих папок остаются в фасаде `opencode_runtime.py`.
- `benchmark_report.py` — оркестрация, печать сводок, запись отчёта в базу.
- `db.py` — SQLite-схема (`SCHEMA`) и весь DB API. `connect()` включает WAL +
  `busy_timeout=5000` + foreign keys — это сознательно, чтобы параллельные
  bench.py/check_models/pricing не ловили «database is locked».
- `pricing.py` — каталог цен поверх OpenRouter SDK (публичный `/models`, кэш в
  таблицах `openrouter_cache*`, TTL 24ч) + ручные цены из `price_overrides`/
  `price_aliases`/`provider_notes`. `get_pricing(refresh=False)` ходит только в
  кэш базы — это режим детерминированной сборки индекса.
- `usage.py` — извлечение токенов из ответов сессии и локальная оценка стоимости.
- `model_catalog.py` — каталог моделей opencode через `opencode models` (subprocess,
  без подъёма `serve`); используется `check_models.py` для перечисления пар provider/model.
- `artifacts.py` — сбор файлов-результатов агента и логов, дедупликация по sha256.
- `lint_metrics.py` — метрика качества кода (#100 Python → #101 мультиязык):
  число diagnostics линтеров в собранных артефактах каждой успешно завершившейся
  копии (`code==0`). Реестр `LINTERS` сопоставляет расширение → линтер; первый
  набор утверждён ПО ФАКТУ накопленных в базе не-Python артефактов: `.py`→Ruff
  (`ruff check --isolated --no-cache --output-format=json`), `.html`/`.htm`→HTML
  Tidy (`tidy -q -e`, каждая строка `line N column M - Error|Warning:` = ошибка),
  `.json`→`jq empty` (невалидный = 1 ошибка/файл). Запускаются ТОЛЬКО линтеры, для
  которых в копии есть подходящие файлы; линтеры файлы модели НЕ исправляют.
  Статусы (на каждый линтер отдельно): `checked` (число ошибок) / `na` (нет
  подходящих файлов, это НЕ ноль) / `unavailable` (инструмент отсутствует/упал —
  бенчмарк живёт дальше, результаты остальных линтеров не скрываются). Diagnostics
  разных инструментов не смешиваются — раздельные счётчики по имени линтера.
  Считается в `_summarize` ПО собранным артефактам ДО `cleanup_collected_artifacts`
  (#99), но логически независима от cleanup; неуспешные копии в агрегат не входят.
  Результат: `runs[].linters` (dict имя→{status,errors}) + `lint_summary` (сводка
  по инструментам). `runs[].ruff`/`ruff_summary` сохранены как синонимы линтера
  `ruff` — обратная совместимость индекса #100 (raw_json → индекс, байт-в-байт).
  Новый язык = добавить `LinterSpec` в `LINTERS`, старые поля не трогать.
- `utils.py` — мелкие хелперы вне слоёв (напр. `json_loads_or` — безопасный
  `json.loads` с откатом на дефолт).
- `index_builder.py` — собирает `docs/data/index.json` из базы (с рейтингом
  моделей и группировкой по проектам); активный denylist отсекается прямо в SQL.
  Рейтинг (#121) СУММИРУЕТ прогоны по ВСЕМ отчётам ячейки (project, provider,
  model) — не latest-wins; строка несёт `successful_run_count`/`total_run_count`/
  `success_rate`. Фейлы модель НЕ исключают (скрывается только модель без единого
  успешного прогона); бейдж unstable — чисто визуальный, на метрики не влияет;
  `unstable_projects` = проекты с хотя бы одним фейлом. Сводка проекта
  (`group_by_project`) — тоже по всем отчётам.
  Успех для рейтинга (#142) = `code==0` И копия сохранила ≥1 `agent_file`
  (`_is_successful_run`): `code==0` без файла модели — не успех, результата нет.
  ИСКЛЮЧЕНИЕ — `--questions-only` (`planning.questions_only` в отчёте,
  `_expects_agent_file`): фазы build там нет, файла никто не ждёт, такие копии
  остаются успехом и в `no_artifact_count` не идут.
  Артефактов в `raw_json` нет, поэтому факт читается из `run_artifacts`
  (`_load_agent_file_runs`; читает FAIL-CLOSED — ошибка чтения роняет сборку, а
  не публикует индекс с нулевыми успехами) и подмешивается в in-memory отчёт служебным
  `runs[]._has_agent_file` — как `_report_id`; перед записью индекса ключ
  снимается (`_strip_internal_run_fields`), в `index.json` только публичные поля.
  Такие копии остаются в `total_run_count` и попадают в `no_artifact_count`
  проекта (витрина: бейдж %OK вычитает его, строка «без артефакта: N»).
- `dashboard_server.py` — локальный статический сервер `docs/`, пересобирает
  индекс при изменении mtime базы (по `_db_fingerprint`).
- `check_models.py` — отдельный диагностический CLI (доступность моделей), делит
  runtime с бенчмарком.
- `scripts/` — CLI-обёртки и разовые утилиты обслуживания базы. Регулярные:
  `build_index.py` (для CI Pages), `run_artifacts.py` (экспорт), `model_exclusions.py`
  (denylist). Чинилки/миграции (запускать осознанно, читать docstring перед прогоном):
  `backfill_runs.py` (ДОЗАПИСАТЬ недостающие прогоны до N успешных на ячейку;
  ничего не удаляет — гонит bench.py на `-n need` и пересчитывает сумму из базы;
  `cell_ok` считает успех ТЕМ ЖЕ правилом, что рейтинг — code=0 + agent_file,
  кроме questions-only (#142): иначе backfill не увидит недобор дашборда),
  `delete_reports.py` (выборочное РУЧНОЕ удаление: отчёт / копия `--run-idx` /
  вся модель / проект целиком; dry-run по умолчанию, `--apply` удаляет; workflow
  перезаписи: `scripts/delete_reports.py` вручную → `bench.py`/`backfill_runs.py`
  дозаписывают заново), `cleanup_runs.py` /
  `cleanup_false_timeouts.py` (чистка ошибочных и ложно-таймаутных прогонов),
  `cleanup_result_dir.py` (безопасная чистка `data/result/`: dry-run по умолчанию,
  `--apply` удаляет подтверждённые по SHA файлы и orphan work_dir старше 24 часов).
  `prepare_work_dirs` перед запуском автоматически подметает такие orphan-хвосты;
  живые процессы защищены `.bench-active.json` с PID,
  `regenerate_raw_json.py` (пересобрать `raw_json` после ручной правки `runs`),
  `restore_reports_from_git.py` (перенести отчёты из закоммиченной копии базы),
  `retry_failed.sh` (перепрогон ошибочных моделей).

## Конфигурация и инварианты

- `opencode.json` (проектный) — только ограничения стандартных агентов
  `plan`/`build`; их системные промпты не переопределяются. `webfetch`/`websearch`
  запрещены, внешние каталоги запрещены кроме штатного каталога plan-файлов
  OpenCode. Провайдеры
  и API-ключи берутся из
  **глобального** opencode-конфига (`~/.config/opencode/`,
  `~/.local/share/opencode/auth.json`) — правь их там, не здесь.
- Отчёты идемпотентны по ключу `(project, provider, model, started_at)` —
  повторный `upsert_report` обновляет строку, `runs` перезаписываются целиком.
- Успешный прогон = `code==0` в ЗАКОММИЧЕННОМ отчёте (транзакция `save_report`
  гарантирует «все артефакты записаны»; оборванная запись отчёта не оставляет)
  И копия сохранила хотя бы один `agent_file` (#142): `code==0` значит лишь
  «агент дошёл до idle и не упал», а бенчмарк меряет РЕЗУЛЬТАТ — все задания
  библиотеки требуют файл. Копия `code==0` без файла — не успех, но и не
  ошибка: НЕ новый код в `RUN_CODES` (её нельзя вводить — таксономию делит
  `check_models`), а отдельный счётчик `summary.no_artifact`
  (`_count_copies_without_agent_file`) и `no_artifact_count` в индексе.
  Повреждённое/прерванное/лимитнутое НЕ чиним — только перепрогон с нуля.
- АВТОУДАЛЕНИЕ отчётов запрещено (#121): backfill только дозаписывает; удаление —
  исключительно ручное решение через `scripts/delete_reports.py`, после чего
  `bench.py`/`backfill_runs.py` дозаписывают заново.
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
