"""Engine tests: run with `python -m pytest` from the plugin folder. No Hermes needed."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import CulpritError, Engine  # noqa: E402

PY = f'"{sys.executable}"'
CHECK = f"{PY} check.py"


@pytest.fixture()
def env(tmp_path):
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "check.py").write_text(
        "import sys\nfrom calc import add\nsys.exit(0 if add(2, 2) == 4 else 1)\n", encoding="utf-8"
    )
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "notes.txt").write_text("start\n", encoding="utf-8")
    engine = Engine(tmp_path / "data")
    engine.sync(str(ws))
    return engine, ws


def edit(engine, ws, name, text, tool="write_file"):
    (ws / name).write_text(text, encoding="utf-8")
    return engine.sync(str(ws), kind="step", tool=tool, summary=name)


def test_first_sync_is_baseline_and_unchanged_sync_records_nothing(env):
    engine, ws = env
    steps = engine.steps(str(ws))
    assert [s["kind"] for s in steps] == ["baseline"]
    assert engine.sync(str(ws), kind="step", tool="read_file") is None
    assert len(engine.steps(str(ws))) == 1


def test_bisect_finds_the_breaking_step(env):
    engine, ws = env
    for i in range(1, 6):
        edit(engine, ws, "notes.txt", f"note {i}\n")
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n", tool="patch")
    for i in range(6, 12):
        edit(engine, ws, "notes.txt", f"note {i}\n")
    seen = []
    result = engine.bisect(str(ws), CHECK, timeout=60, progress=seen.append)
    assert result["status"] == "found"
    assert result["culprit"]["step"] == 6
    assert result["culprit"]["tool"] == "patch"
    assert result["culprit"]["last_good_step"] == 5
    assert "calc.py" in result["diff"]["files"]
    assert "return a - b" in result["diff"]["patch"]
    # 13 states: two end checks plus at most four halvings
    assert result["test_runs"] <= 6
    assert len(seen) == result["test_runs"]
    # the live workspace is untouched by bisecting
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "note 11\n"
    assert engine.last_run(str(ws))["culprit"]["step"] == 6


def test_revert_undoes_only_the_culprit(env):
    engine, ws = env
    edit(engine, ws, "notes.txt", "kept before\n")
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n")
    edit(engine, ws, "notes.txt", "kept after\n")
    assert engine.bisect(str(ws), CHECK, timeout=60)["culprit"]["step"] == 2
    done = engine.revert(str(ws))
    assert done["reverted_step"] == 2
    assert done["files"] == ["calc.py"]
    assert "a + b" in (ws / "calc.py").read_text(encoding="utf-8")
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "kept after\n"
    assert engine.bisect(str(ws), CHECK, timeout=60)["status"] == "passing"
    assert engine.steps(str(ws))[-1]["kind"] == "revert"


def test_conflicting_revert_changes_nothing(env):
    engine, ws = env
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n")
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a * b  # rewritten\n")
    before = (ws / "calc.py").read_bytes()
    with pytest.raises(CulpritError, match="No file was changed"):
        engine.revert(str(ws), 1)
    assert (ws / "calc.py").read_bytes() == before


def test_passing_and_failing_at_start(env):
    engine, ws = env
    edit(engine, ws, "notes.txt", "x\n")
    assert engine.bisect(str(ws), CHECK, timeout=60)["status"] == "passing"
    result = engine.bisect(str(ws), f"{PY} -c \"import sys; sys.exit(3)\"", timeout=60)
    assert result["status"] == "failing_at_start"
    assert result["culprit"] is None


def test_no_history_and_empty_command(env):
    engine, ws = env
    assert engine.bisect(str(ws), CHECK)["status"] == "no_history"
    with pytest.raises(CulpritError):
        engine.bisect(str(ws), "   ")


def test_outside_edits_are_their_own_step(env):
    engine, ws = env
    edit(engine, ws, "notes.txt", "tool edit\n")
    (ws / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")  # the person, not a tool
    result = engine.bisect(str(ws), CHECK, timeout=60)
    assert result["status"] == "found"
    assert result["culprit"]["kind"] == "external"


def test_deleted_and_new_files_round_trip(env):
    engine, ws = env
    (ws / "calc.py").unlink()
    engine.sync(str(ws), kind="step", tool="terminal", summary="rm calc.py")
    edit(engine, ws, "extra.txt", "new\n")
    result = engine.bisect(str(ws), CHECK, timeout=60)
    assert result["culprit"]["step"] == 1
    engine.revert(str(ws), 1)
    assert (ws / "calc.py").exists() and (ws / "extra.txt").exists()


def test_mark_limits_the_range(env):
    engine, ws = env
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n")
    engine.mark(str(ws))
    edit(engine, ws, "notes.txt", "later\n")
    assert engine.bisect(str(ws), CHECK, timeout=60)["status"] == "failing_at_start"
    assert engine.bisect(str(ws), CHECK, timeout=60, since_mark=False)["culprit"]["step"] == 1


def test_secrets_and_dependency_folders_are_not_snapshotted(env):
    engine, ws = env
    (ws / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "big.js").write_text("x", encoding="utf-8")
    assert engine.sync(str(ws), kind="step", tool="terminal") is None


def test_timeout_counts_as_failure(env):
    engine, ws = env
    edit(engine, ws, "notes.txt", "x\n")
    result = engine.bisect(str(ws), f"{PY} -c \"import time; time.sleep(30)\"", timeout=1)
    assert result["probes"][0]["timed_out"] is True
    assert result["status"] == "failing_at_start"


def test_state_survives_a_restart(env, tmp_path):
    engine, ws = env
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n")
    again = Engine(tmp_path / "data")
    assert len(again.steps(str(ws))) == 2
    assert again.bisect(str(ws), CHECK, timeout=60)["culprit"]["step"] == 1


def test_refuses_home_and_missing_folders(tmp_path):
    engine = Engine(tmp_path / "data")
    with pytest.raises(CulpritError):
        engine.sync(str(Path.home()))
    with pytest.raises(CulpritError):
        engine.sync(str(tmp_path / "nope"))
    with pytest.raises(CulpritError):
        engine.sync("")


def test_clear_removes_everything(env, tmp_path):
    engine, ws = env
    edit(engine, ws, "notes.txt", "x\n")
    assert engine.clear(str(ws))["deleted_steps"] == 2
    assert not any((tmp_path / "data" / "repos").glob("*.git"))
    assert engine.sync(str(ws))["kind"] == "baseline"


def test_workspace_inside_a_user_repo_is_left_alone(env):
    engine, ws = env
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    head_before = sorted(p.name for p in (ws / ".git").iterdir())
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n")
    assert engine.bisect(str(ws), CHECK, timeout=60)["culprit"]["step"] == 1
    engine.revert(str(ws))
    assert sorted(p.name for p in (ws / ".git").iterdir()) == head_before
    assert not (ws / ".git" / "index").exists()


@pytest.mark.skipif(os.name != "nt", reason="junction cleanup is Windows specific")
def test_linked_dependency_folder_is_never_deleted(env):
    engine, ws = env
    dep = ws / "node_modules"
    dep.mkdir()
    (dep / "keep.txt").write_text("keep", encoding="utf-8")
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n")
    engine.bisect(str(ws), CHECK, timeout=60)
    assert (dep / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_test_command_and_git_get_a_minimal_environment(env, monkeypatch):
    engine, ws = env
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("MY_PROJECT_DB", "sqlite://x")
    monkeypatch.setenv("LC_ALL", "C")
    (ws / "dump.py").write_text(
        "import json, os, sys\njson.dump(dict(os.environ), open(os.environ['OUT'], 'w'))\n"
        "from calc import add\nsys.exit(0 if add(2, 2) == 4 else 1)\n", encoding="utf-8")
    engine.sync(str(ws), kind="step", tool="write_file")
    engine.mark(str(ws))  # search from the state that already has dump.py
    edit(engine, ws, "calc.py", "def add(a, b):\n    return a - b\n")
    out = ws.parent / "env.json"
    monkeypatch.setenv("OUT", str(out))

    import json
    from engine import Engine, _git_env, minimal_env
    allowed = Engine(ws.parent / "data", pass_env=["OUT", "MY_PROJECT_DB", "bad name", "X;rm"])
    assert allowed.pass_env == ["OUT", "MY_PROJECT_DB"]
    assert allowed.bisect(str(ws), f"{PY} dump.py", timeout=60)["status"] == "found"
    seen = json.loads(out.read_text(encoding="utf-8"))
    upper = {k.upper() for k in seen}
    assert "OPENROUTER_API_KEY" not in upper and "ANTHROPIC_API_KEY" not in upper
    assert "GIT_SSH_COMMAND" not in upper
    assert seen["MY_PROJECT_DB"] == "sqlite://x" and seen["CULPRIT"] == "1"
    assert "PATH" in upper and "LC_ALL" in upper

    plain = minimal_env()
    assert not any("API_KEY" in k.upper() or k.upper() in ("OUT", "MY_PROJECT_DB") for k in plain)
    git_env = _git_env(ws.parent / "g", ws)
    assert not any("API_KEY" in k.upper() for k in git_env)
    assert "GIT_SSH_COMMAND" not in git_env and git_env["GIT_TERMINAL_PROMPT"] == "0"


def test_long_or_multiline_commands_are_refused(env):
    engine, ws = env
    edit(engine, ws, "notes.txt", "x\n")
    with pytest.raises(CulpritError, match="longer than"):
        engine.bisect(str(ws), "echo " + "a" * 400)
    with pytest.raises(CulpritError, match="single line"):
        engine.bisect(str(ws), "echo a\necho b")
