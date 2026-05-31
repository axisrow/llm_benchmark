# CLAUDE.md

Короткая справка для старого Claude workflow. Актуальные правила для Codex см. в
`AGENTS.md`.

## Назначение

Проект запускает параллельные копии `opencode serve` для одной задачи и пишет
результаты в `data/main.db`. База — единственный источник правды для отчётов,
библиотеки заданий, цен, правил бесплатности и кэша OpenRouter.

## Команды

```bash
python bench.py --project hello_world "напиши hello world на питоне"
python bench.py --project hello_world -n 3 -p zai-coding-plan -m glm-5.1 "..."
python bench.py --project my_task -f task.txt
python bench.py serve --port 8000
python check_models.py --provider opencode
python check_models.py --models zai-coding-plan/glm-5.1
python scripts/build_index.py
python -m py_compile bench.py
python3 -m pytest
```

## Структура

- `bench.py` — тонкий CLI.
- `opencode_runtime.py` — запуск `opencode serve`, HTTP/SSE сессии, пути рабочих папок.
- `benchmark_report.py` — параллельные копии, отчётность, запись в SQLite.
- `dashboard_server.py` и `index_builder.py` — локальный сайт и генерация `docs/data/index.json`.
- `db.py` — SQLite schema и общий DB API.
- `scripts/run_artifacts.py` — экспорт артефактов из базы.

`scripts/build_index.py` оставлен как совместимая CLI-обёртка для GitHub Pages.
