"""Local dashboard server backed by data/main.db."""

import functools
import http.server
import socketserver
import sys
from pathlib import Path

from db import DB_PATH, PROJECT_ROOT
from index_builder import build_index


def cleanup_index_snapshot(index_path: Path) -> None:
    try:
        index_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"[serve] не удалось удалить {index_path}: {exc}", file=sys.stderr)


def _db_fingerprint() -> float:
    newest = 0.0
    for path in (DB_PATH, DB_PATH.with_name(DB_PATH.name + "-wal")):
        try:
            newest = max(newest, path.stat().st_mtime)
        except FileNotFoundError:
            pass
    return newest


def serve(port: int = 8000) -> None:
    docs_dir = PROJECT_ROOT / "docs"
    index_path = docs_dir / "data" / "index.json"
    last_fp = 0.0
    owns_index_snapshot = False
    try:
        class Handler(http.server.SimpleHTTPRequestHandler):
            def _maybe_rebuild(self) -> None:
                nonlocal last_fp
                if self.path.split("?", 1)[0] != "/data/index.json":
                    return
                fp = _db_fingerprint()
                if fp == last_fp:
                    return
                try:
                    count = build_index()
                    last_fp = fp
                    print(f"[serve] index пересобран ({count} отчётов)", file=sys.stderr)
                except Exception as exc:
                    print(f"[serve] пересборка индекса не удалась: {exc}",
                          file=sys.stderr)

            def do_GET(self):
                self._maybe_rebuild()
                super().do_GET()

            def do_HEAD(self):
                self._maybe_rebuild()
                super().do_HEAD()

        handler = functools.partial(Handler, directory=str(docs_dir))
        with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
            build_index()
            owns_index_snapshot = True
            last_fp = _db_fingerprint()
            print(f"Тестовый сервер: http://localhost:{port}/  (данные из data/main.db)")
            print("Ctrl+C для остановки.")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nОстановлен.")
    finally:
        if owns_index_snapshot:
            cleanup_index_snapshot(index_path)
