"""Local dashboard server backed by data/main.db.

Два режима внутри одного Handler (фабрика :func:`make_dashboard_handler`):
- статика из ``docs/`` (+ авто-пересборка index.json по отпечатку БД);
- локальный JSON-API разметки planning-вопросов (issue #93): GET /api/capabilities,
  PUT/DELETE /api/question-reviews.

API доступен только на 127.0.0.1, same-origin, без CORS — это инструмент
локального разметчика, а не публичный эндпоинт. Запись идёт напрямую в SQLite
короткой транзакцией; ошибки транзакции откатываются без изменения БД.
"""

import functools
import http.server
import json
import socketserver
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import db
from artifacts import delete_project_result_dir, project_has_active_run
from db import DB_PATH, PROJECT_ROOT, delete_question_review, put_question_review
from index_builder import build_index
from utils import sanitize_name

# Корень рабочих каталогов прогонов на диске (data/result). Файловая очистка
# удаляемого проекта (issue #110) идёт строго под ним. Держим модульной ссылкой:
# serve() и API читают её на момент вызова, тесты патчат на временный каталог.
RESULT_ROOT = PROJECT_ROOT / "data" / "result"

# Лимит тела запроса к API (issue #93). Составной ключ + verdict — это сотни байт,
# 16 KiB хватает с огромным запасом и отсекает любой «тяжёлый»/злонамеренный пейлоад.
_MAX_BODY_BYTES = 16 * 1024

# Составной ключ вопроса — ровно PK agent_questions. Любой PUT/DELETE обязан нести
# все пять полей корректных типов; иначе 400.
_REVIEW_INT_FIELDS = ("report_id", "run_idx", "attempt_idx", "question_idx")
_REVIEW_STR_FIELDS = ("request_id",)
_VALID_VERDICTS = ("useful", "unnecessary")


def cleanup_index_snapshot(index_path: Path) -> None:
    try:
        index_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"[serve] не удалось удалить {index_path}: {exc}", file=sys.stderr)


def _db_fingerprint(db_path: Path | None = None) -> float:
    """Самый свежий mtime среди базы и её WAL — триггер авто-пересборки индекса.

    ``db_path=None`` → читает модульный ``DB_PATH`` на момент вызова (НЕ дефолт
    параметра): тесты патчат ``dashboard_server.DB_PATH``, и динамическое чтение
    обязано это подхватывать.
    """
    if db_path is None:
        db_path = DB_PATH
    newest = 0.0
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            newest = max(newest, path.stat().st_mtime)
        except FileNotFoundError:
            pass
    return newest


def _send_json(handler: BaseHTTPRequestHandler, status: int,
               payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    # Намеренно НЕТ CORS-заголовков: API локальный, same-origin.
    handler.end_headers()
    handler.wfile.write(body)


def _parse_review_key(payload: object) -> tuple[dict, str | None]:
    """Валидирует составной ключ вопроса из JSON- payload.

    Возвращает ``(key_dict, error)``: при ошибке ``key_dict`` пуст, ``error`` —
    текст (→ 400). ``verdict`` здесь НЕ валидируется (его требует только PUT).
    """
    if not isinstance(payload, dict):
        return {}, "ожидался JSON-объект"
    key: dict = {}
    for field in _REVIEW_INT_FIELDS:
        value = payload.get(field)
        # bool — это подтип int в Python; отвергаем явно, чтобы True не прошло
        # как question_idx=1.
        if isinstance(value, bool) or not isinstance(value, int):
            return {}, f"поле {field} должно быть целым"
        key[field] = value
    for field in _REVIEW_STR_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            return {}, f"поле {field} должно быть непустой строкой"
        key[field] = value
    return key, None


def _read_json_body(handler: BaseHTTPRequestHandler) -> tuple[object, str | None]:
    """Читает и парсит тело запроса с лимитом размера.

    Возвращает ``(payload, error)``: ``error`` установлен при malformed JSON,
    превышении лимита или пустом теле (→ 400).
    """
    length_header = handler.headers.get("Content-Length")
    if length_header is None:
        # Без Content-Length не читаем — все наши клиенты (fetch) его шлют.
        raw = b""
    else:
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            return None, "некорректный Content-Length"
        if length > _MAX_BODY_BYTES:
            return None, f"тело превышает {_MAX_BODY_BYTES} байт"
        raw = handler.rfile.read(length)
    if not raw:
        return None, "пустое тело запроса"
    try:
        return json.loads(raw.decode("utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "malformed JSON"


def make_dashboard_handler(db_path: Path):
    """Возвращает класс HTTP-Handler, обслуживающий статику + локальный API.

    Фабрика (а не инлайн-класс внутри ``serve``) позволяет тестировать продовый
    Handler напрямую: тесты инстанцируют его с временной БД, не поднимая serve().
    ``db_path`` зафиксирован в замыкании — все API-запросы пишут в одну базу.
    """
    # last_fp замкнут на экземпляр «сервера» (один Handler-класс на serve-сессию);
    # обновляется только при авто-пересборке индекса, чтобы не пересобирать на
    # каждый GET /data/index.json.
    state = {"last_fp": 0.0}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def _maybe_rebuild(self) -> None:
            if self.path.split("?", 1)[0] != "/data/index.json":
                return
            # Без аргумента: читает модульный DB_PATH (тесты патчат именно его и
            # саму _db_fingerprint). Запись в API идёт через замкнутый db_path —
            # в serve() это один и тот же файл.
            fp = _db_fingerprint()
            if fp == state["last_fp"]:
                return
            try:
                count = build_index()
                state["last_fp"] = fp
                print(f"[serve] index пересобран ({count} отчётов)",
                      file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 — как в проде: молча логируется
                print(f"[serve] пересборка индекса не удалась: {exc}",
                      file=sys.stderr)

        # --- статика ---
        def do_GET(self):
            if self._handle_api_get():
                return
            self._maybe_rebuild()
            super().do_GET()

        def do_HEAD(self):
            self._maybe_rebuild()
            super().do_HEAD()

        # --- API: GET /api/capabilities ---
        def _handle_api_get(self) -> bool:
            path = self.path.split("?", 1)[0]
            if path == "/api/capabilities":
                # delete_project (issue #110): доступно только в локальном serve;
                # на GitHub Pages эндпоинта /api/* нет → фронтенд read-only.
                _send_json(self, 200, {"question_reviews": True,
                                       "delete_project": True})
                return True
            if path.startswith("/api/"):
                # неизвестный /api/* → 404 (как обычная статики-ветка, но без
                # попытки отдать файл).
                _send_json(self, 404, {"error": "not found"})
                return True
            return False

        # --- API: PUT/DELETE /api/question-reviews ---
        def do_PUT(self):
            self._handle_review_write(delete=False)

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            # issue #110: DELETE /api/projects/<name> — удаление проекта целиком.
            if path.startswith("/api/projects/"):
                self._handle_project_delete(path)
                return
            self._handle_review_write(delete=True)

        # --- API: DELETE /api/projects/<name> (issue #110) ---
        def _handle_project_delete(self, path: str) -> None:
            raw_name = path[len("/api/projects/"):]
            # Имя приходит URL-кодированным (фронтенд шлёт encodeURIComponent).
            # Декодируем и валидируем: пустое или с разделителями пути/NUL — 400.
            # `/` внутри имени сюда не дойдёт (уже разрезал бы path), а `%2F`/`%2e`
            # раскодируются здесь — отсекаем их как невалидный ввод, не как 404.
            name = urllib.parse.unquote(raw_name)
            if not name or "/" in name or "\\" in name or "\x00" in name \
                    or name in (".", ".."):
                _send_json(self, 400, {"error": "некорректное имя проекта"})
                return

            # Отказ при активном прогоне: живой .bench-active.json marker под
            # data/result/<sanitize(name)>/ означает, что процесс сейчас пишет в
            # папку проекта — удалять нельзя (409). Проверяем ДО транзакции.
            disk_name = sanitize_name(name)
            try:
                if project_has_active_run(RESULT_ROOT, disk_name):
                    _send_json(self, 409, {"error": "у проекта есть активный прогон"})
                    return
            except Exception as exc:  # noqa: BLE001 — проверка не должна валить API
                print(f"[api] active-run check failed: {exc}", file=sys.stderr)

            conn = db.connect(db_path)
            try:
                with conn:
                    result = db.delete_project(conn, name)
                if not result["existed"]:
                    # Несуществующий проект → 404, не частичный успех. Транзакция
                    # ничего не удалила (delete_project идемпотентен).
                    _send_json(self, 404, {"error": "проект не найден"})
                    return
            except Exception as exc:  # noqa: BLE001 — 500 без изменения БД (rollback)
                print(f"[api] DELETE project failed: {exc}", file=sys.stderr)
                _send_json(self, 500, {"error": "внутренняя ошибка"})
                return

            # Файловую очистку делаем ТОЛЬКО после успешного commit БД: даже если
            # она частично не удалась, БД уже консистентна (orphan-файлы подметёт
            # штатный cleanup_result_dir позже). Не роняем ответ из-за файлов.
            try:
                delete_project_result_dir(RESULT_ROOT, disk_name)
            except Exception as exc:  # noqa: BLE001
                print(f"[api] cleanup data/result for {name!r} failed: {exc}",
                      file=sys.stderr)

            _send_json(self, 200, {
                "project": name,
                "reports": result["reports"],
                "runs": result["runs"],
                "artifacts": result["artifacts"],
            })

        def do_POST(self):
            # POST к reviews не поддерживается (только PUT/DELETE) → 405.
            path = self.path.split("?", 1)[0]
            if path == "/api/question-reviews":
                _send_json(self, 405, {"error": "method not allowed"})
            else:
                _send_json(self, 404, {"error": "not found"})

        def _handle_review_write(self, *, delete: bool) -> None:
            path = self.path.split("?", 1)[0]
            if path != "/api/question-reviews":
                _send_json(self, 404, {"error": "not found"})
                return
            payload, err = _read_json_body(self)
            if err is not None:
                _send_json(self, 400, {"error": err})
                return
            key, err = _parse_review_key(payload)
            if err is not None:
                _send_json(self, 400, {"error": err})
                return

            if delete:
                self._apply_delete(key)
                return

            # PUT: нужен verdict.
            verdict = payload.get("verdict") if isinstance(payload, dict) else None
            if verdict not in _VALID_VERDICTS:
                _send_json(self, 400, {"error": "verdict должен быть useful или unnecessary"})
                return
            self._apply_put(key, verdict)

        def _apply_put(self, key: dict, verdict: str) -> None:
            conn = db.connect(db_path)
            try:
                with conn:
                    try:
                        row = put_question_review(
                            conn,
                            report_id=key["report_id"], run_idx=key["run_idx"],
                            attempt_idx=key["attempt_idx"],
                            request_id=key["request_id"],
                            question_idx=key["question_idx"], verdict=verdict,
                        )
                    except LookupError:
                        # вопроса нет в agent_questions → 404 (транзакция пуста,
                        # БД не изменилась).
                        _send_json(self, 404, {"error": "вопрос не найден"})
                        return
                # with conn уже закоммитил; успешный ответ — сохранённый verdict.
                _send_json(self, 200, {"verdict": row["verdict"]})
            except Exception as exc:  # noqa: BLE001 — 500 без изменения БД
                # with conn при исключении откатил транзакцию.
                print(f"[api] PUT review failed: {exc}", file=sys.stderr)
                _send_json(self, 500, {"error": "внутренняя ошибка"})

        def _apply_delete(self, key: dict) -> None:
            conn = db.connect(db_path)
            try:
                with conn:
                    delete_question_review(
                        conn,
                        report_id=key["report_id"], run_idx=key["run_idx"],
                        attempt_idx=key["attempt_idx"],
                        request_id=key["request_id"],
                        question_idx=key["question_idx"],
                    )
                _send_json(self, 200, {"verdict": None})
            except Exception as exc:  # noqa: BLE001 — 500 без изменения БД
                print(f"[api] DELETE review failed: {exc}", file=sys.stderr)
                _send_json(self, 500, {"error": "внутренняя ошибка"})

        def log_message(self, *args):
            pass  # тихо

    return Handler


def serve(port: int = 8000) -> None:
    docs_dir = PROJECT_ROOT / "docs"
    index_path = docs_dir / "data" / "index.json"
    owns_index_snapshot = False
    try:
        handler = functools.partial(make_dashboard_handler(DB_PATH),
                                    directory=str(docs_dir))
        with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
            build_index()
            owns_index_snapshot = True
            print(f"Тестовый сервер: http://localhost:{port}/  "
                  f"(данные из data/main.db)")
            print("Ctrl+C для остановки.")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nОстановлен.")
    finally:
        if owns_index_snapshot:
            cleanup_index_snapshot(index_path)
