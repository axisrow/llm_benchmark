import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_bench_planner_agent_configuration() -> None:
    config = json.loads(
        (PROJECT_ROOT / "opencode.json").read_text(encoding="utf-8")
    )

    planner = config["agent"]["bench_planner"]
    assert planner["mode"] == "primary"
    assert planner["model"] == "zai-coding-plan/glm-5.1"
    assert planner["permission"] == {
        "question": "allow",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "lsp": "allow",
        "bash": "deny",
        "edit": "deny",
        "write": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "task": "deny",
        "external_directory": "deny",
        "doom_loop": "deny",
    }

    # Security-инвариант: planner обязан быть read-only + question. Эти ключи
    # — load-bearing граница безопасности; именуем их явно, чтобы регрессия
    # (напр. случайно добавленный `bash: allow`) падала с понятным сообщением,
    # а не только через snapshot-equality выше.
    DENY = {
        "bash", "edit", "write",
        "webfetch", "websearch",
        "task", "external_directory",
    }
    for key in DENY:
        assert planner["permission"][key] == "deny", (
            f"{key} must be denied for bench_planner"
        )
