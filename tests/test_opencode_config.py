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
    }
