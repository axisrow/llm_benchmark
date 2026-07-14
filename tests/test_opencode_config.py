import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_native_plan_and_build_agent_overrides() -> None:
    config = json.loads(
        (PROJECT_ROOT / "opencode.json").read_text(encoding="utf-8")
    )

    agents = config["agent"]
    assert set(agents) == {"plan", "build"}
    assert "model" not in agents["plan"]
    assert "model" not in agents["build"]

    planner = agents["plan"]
    # Benchmark permissions may constrain the native agents, but project-level
    # prompts would change the task being benchmarked. Keep native OpenCode
    # plan/build prompts untouched.
    assert "prompt" not in planner
    assert planner["permission"]["question"] == "allow"
    assert planner["permission"]["plan_exit"] == "allow"
    # Do not override native edit rules: native plan restricts edits to
    # .opencode/plans/*.md. A project-level catch-all allow would be merged
    # later and silently remove that boundary.
    assert "edit" not in planner["permission"]
    assert "write" not in planner["permission"]
    assert planner["permission"]["task"] == "deny"
    assert planner["permission"]["bash"] == "deny"
    assert planner["permission"]["webfetch"] == "deny"
    assert planner["permission"]["websearch"] == "deny"
    assert planner["permission"]["external_directory"] == {
        "*": "deny",
        "~/.local/share/opencode/plans/*": "allow",
    }

    builder = agents["build"]
    assert "prompt" not in builder
    assert builder["permission"]["question"] == "deny"
    assert builder["permission"]["bash"] == "allow"
    assert builder["permission"]["task"] == "deny"
    assert builder["permission"]["edit"] == {
        "*": "allow",
        ".opencode/plans/*": "deny",
        "~/.local/share/opencode/plans/*": "deny",
    }
    assert builder["permission"]["webfetch"] == "deny"
    assert builder["permission"]["websearch"] == "deny"
    assert builder["permission"]["external_directory"] == {
        "*": "deny",
        "~/.local/share/opencode/plans/*": "allow",
    }
