# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Назначение проекта

Тонкая Python-обёртка над `opencode-ai` SDK. Запускает локального автономного кодинг-агента поверх `opencode serve`. За один прогон поднимается **N параллельных копий** одной задачи (по умолчанию 5), каждая — в собственной изолированной рабочей папке под `data/result/` со своим `opencode serve` на отдельном порту. opencode стартует именно в этой папке — поэтому агент не видит файлы запускалки и не может выйти наверх (`external_directory: deny`). Несколько копий полезны, чтобы сравнить варианты решения одной модели.

## Источник правды для настроек

Конфиги opencode мерджатся по приоритету (от низкого к высокому): глобальный → custom (через `OPENCODE_CONFIG`) → проектный. Поэтому в проекте держим **только** то, чего нет глобально:

| Что | Где |
|---|---|
| Провайдеры (ollama, openrouter, opencode zen, …) | `~/.config/opencode/opencode.json` |
| API-ключи | `~/.local/share/opencode/auth.json` (через `opencode auth login`) |
| Дефолтная модель | `~/.config/opencode/opencode.json` (ключ `"model"`) |
| Описание агента `coder` (промпт, разрешения) | `opencode.json` в корне проекта |
| MCP-серверы | `~/.config/opencode/opencode.json` |

Проектный `opencode.json` намеренно не содержит блока `provider` и `model` — всё это уже есть глобально.

## Команды

```bash
# Установка зависимостей (Python 3.12+)
pip install -r requirements.txt

# Запуск задачи (флаг --project обязателен; по умолчанию 5 параллельных копий)
python agent.py --project hello_world "напиши hello world на питоне в файл hello.py"

# Число копий
python agent.py --project hello_world -n 3 "..."

# Замер времени
time python agent.py --project hello_world "..."

# Сменить модель (provider и model передаются раздельно)
python agent.py --project demo -p ollama -m gemma4:31b-cloud "..."
python agent.py --project demo -p zai-coding-plan -m glm-5.1 "..."

# Задача из файла
python agent.py --project my_task -f task.txt

# Жёсткий таймаут на одну копию (по умолчанию 120с, с момента создания сессии)
python agent.py --project my_task --timeout 30 "..."

# Порт первой копии (остальные +1); по умолчанию 4096
python agent.py --project my_task --base-port 5000 "..."

# Проверка синтаксиса
python -m py_compile agent.py
```

Тестового фреймворка нет. Линтер не настроен.

## Где живут результаты

Результаты сгруппированы по проекту: `data/result/<project>/<provider>_<model>/`. Папка проекта одна на проект, внутри — по подпапке на каждую модель (провайдер в имени снимает коллизию одной модели у разных провайдеров, напр. `glm-4.7` у `ollama-cloud` и `zai-coding-plan`). В папке модели лежит её `report.json` и по подпапке на каждую копию: `<YYYYMMDD>-<HHMMSS>_<N>/`, где `<YYYYMMDD>-<HHMMSS>` — дата и время старта прогона (общие на прогон), а `<N>` — индекс копии (`_1.._N`). Пример: `data/result/hello_world/zai-coding-plan_glm-5.1/20260529-010500_1/ … _5/`. Все файлы агента оказываются внутри подпапки копии — это её «корень мира». Подробный прогресс копии (текст модели, tool calls) пишется в `run.log` внутри её подпапки; в общий stdout идёт только краткий статус по каждой копии и финальный отчёт.

В конце прогона печатается **отчёт по времени**: таблица «копия / статус / время» (время каждой копии меряется от входа в `run_copy`, включая старт её `opencode serve`, до завершения), плюс итоги — общее wall-clock прогона, минимальное/максимальное/среднее время копий и сводка `N готово / M таймаут / K ошибка`. Тот же отчёт в машиночитаемом виде сохраняется в `report.json` в папке модели `data/result/<project>/<provider>_<model>/` (поля `project, model, provider, prompt, description, copies, started_at, run_elapsed, summary, pricing, runs[]`). Один `report.json` на модель; общий вид по проекту собирает `scripts/build_index.py`, группируя отчёты по полю `project`.

## Архитектура запуска (agent.py)

1. `prepare_work_dirs(project, provider, model, copies)` — санитизирует имена (`:`, `/`, `\` → `-`), создаёт папку прогона `data/result/<project>/<provider>_<model>/` и под ней N подпапок `<YYYYMMDD>-<HHMMSS>_<i>` (общие дата+время старта на прогон). Возвращает список путей.
2. `main()` — оркестратор: через `ThreadPoolExecutor(max_workers=copies)` запускает по `run_copy` на каждую папку. Порт копии — `base_port + i` (по умолчанию 4096, 4097, …). Ждёт все копии, печатает сводку (`N готово / M таймаут / K ошибка`) и выходит с **максимальным** (худшим) кодом среди копий.
3. `run_copy(index, work_dir, port, …)` — один прогон: открывает `run.log` в `work_dir`, поднимает сервер, гоняет задачу, возвращает код копии. Подробный вывод идёт в `run.log` через writer; в stdout — короткий статус с локом от перемешивания.
4. `ensure_server_running(work_dir, port, status)` — проверяет, отвечает ли `opencode serve` на `port`. Если нет — форкает `opencode serve --port <port>` с `cwd=work_dir` и `env[OPENCODE_CONFIG]=<абс.путь к проектному opencode.json>`. stderr пишется во временный файл; при крахе/таймауте лог уходит в статус, копия возвращает код 2. Все поднятые серверы хранятся в списке `_server_processes`; `atexit`-обработчик `_stop_servers` гасит их все при выходе.
5. `run_task(…, port, write)` — общается с сервером на `port` **напрямую по HTTP**, минуя устаревший Python-SDK. Весь подробный прогресс пишется через `write` (в `run.log` копии), а не в stdout:
   - `POST /session` — создать сессию.
   - В **фоновом потоке** открывает SSE-стрим `GET /event`, фильтрует события по нашему `sessionID`, пишет читаемый прогресс (текст модели, tool calls). Когда приходит `session.idle` или `session.error` — ставит `done`, поток завершается.
   - Основной поток: `POST /session/{id}/message` с телом `{agent, model: {providerID, modelID}, parts}` — синхронный, ждёт ответ сервера.
   - **Сразу проверяет ответ на ошибку:** HTTP-код ≥ 400 или `info.error` в теле (ошибка провайдера приходит при HTTP 200) → пишет текст и возвращает код `2`, не дожидаясь `session.idle`. Иначе ждёт `done.wait()` до общего дедлайна `--timeout`. `session.error` из SSE тоже отдаёт код `2`. Это устраняет «зависание» на 120с, когда провайдер недоступен/неоплачен.

**Почему минуем SDK.** `opencode-ai 0.1a36` использует устаревшую плоскую схему (`mode/modelID/providerID`), которую сервер новой версии игнорит и сбрасывает в дефолтный агент `build` с глобальной дефолтной моделью. Прямой POST с новой схемой работает корректно.

`OPENCODE_CONFIG` — документированный механизм opencode: путь к конфигу не зависит от cwd, поэтому конфиг можно держать в корне проекта, а сервер запускать в любой подпапке.

## Дефолты (agent.py)

- `DEFAULT_BASE_PORT = 4096` (порт первой копии; копия `i` использует `base_port + i`). Переопределяется флагом `--base-port`.
- `DEFAULT_COPIES = 5` — число параллельных копий по умолчанию (флаг `-n/--copies`).
- `DEFAULT_MODEL = "glm-5.1"`, `DEFAULT_PROVIDER = "zai-coding-plan"`, `DEFAULT_AGENT = "coder"`. Агент `coder` в проектном `opencode.json` также прописан на `zai-coding-plan/glm-5.1`. (Провайдер `opencode` zen для glm-5.1 не годится: модели там нет и он отдаёт `401 No payment method`.)
- `--timeout` — жёсткий таймаут на **одну** копию (по умолчанию 120с).

## Стиль

Python 3.12+, type hints, PEP 8 (см. `AGENTS.md`).
