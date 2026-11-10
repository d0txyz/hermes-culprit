"""Plugin-surface tests with a stand-in for the Hermes plugin context."""

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

PY = f'"{sys.executable}"'
CHECK = f"{PY} check.py"


class FakeCtx:
    def __init__(self, cwd, settings=None):
        self.cwd = str(cwd)
        self.settings = settings or {}
        self.tools, self.hooks, self.commands, self.cli = {}, {}, {}, {}
        self.posted = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def session_cwd(self):
        return self.cwd

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        assert schema["name"] == name
        self.tools[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint="", **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def post_message(self, text):
        ctx = self

        class Handle:
            ok = True

            def edit(self, new_text):
                ctx.posted.append(new_text)

        self.posted.append(text)
        return Handle()


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "check.py").write_text(
        "import sys\nfrom calc import add\nsys.exit(0 if add(2, 2) == 4 else 1)\n", encoding="utf-8"
    )
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    module = importlib.import_module(ROOT.name)
    ctx = FakeCtx(ws)
    module.register(ctx)
    ctx.hooks["on_session_start"](session_id="s1")
    return ctx, ws


def call(ctx, tool, args):
    return json.loads(ctx.tools[tool](args, session_id="s1"))


def tool_edit(ctx, ws, name, text, tool="write_file"):
    assert ctx.hooks["pre_tool_call"](tool_name=tool, args={"path": name}, task_id="t") is None
    (ws / name).write_text(text, encoding="utf-8")
    ctx.hooks["post_tool_call"](tool_name=tool, args={"path": name}, result="{}", task_id="t", session_id="s1")


def test_manifest_matches_registrations(plugin):
    ctx, _ws = plugin
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    for name in ctx.tools:
        assert f"  - {name}\n" in manifest
    for name in ctx.hooks:
        assert f"  - {name}\n" in manifest
    assert set(ctx.commands) == {"culprit"} and set(ctx.cli) == {"culprit"}


def test_hooks_record_steps_and_tools_find_the_culprit(plugin):
    ctx, ws = plugin
    tool_edit(ctx, ws, "notes.txt", "one\n")
    tool_edit(ctx, ws, "calc.py", "def add(a, b):\n    return a - b\n", tool="patch")
    tool_edit(ctx, ws, "notes.txt", "two\n")
    ctx.hooks["post_tool_call"](tool_name="read_file", args={"path": "calc.py"}, result="{}", task_id="t")

    timeline = call(ctx, "culprit_timeline", {})
    assert [s["tool"] for s in timeline["steps"]] == ["", "write_file", "patch", "write_file"]

    found = call(ctx, "culprit_bisect", {"test_command": CHECK})
    assert found["status"] == "found" and found["culprit"]["step"] == 2
    assert found["culprit"]["summary"] == "calc.py"

    shown = call(ctx, "culprit_timeline", {"show_step": 2})
    assert "return a - b" in shown["diff"]["patch"] and shown["last_culprit"]["step"] == 2

    done = call(ctx, "culprit_revert", {})
    assert done["ok"] is True and done["reverted_step"] == 2
    assert call(ctx, "culprit_bisect", {"test_command": CHECK})["status"] == "passing"
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "two\n"


def test_model_triggered_bisect_and_revert_ask_for_approval(plugin):
    ctx, _ws = plugin
    for name in ("culprit_bisect", "culprit_revert"):
        directive = ctx.hooks["pre_tool_call"](tool_name=name, args={}, task_id="t")
        assert directive["action"] == "approve" and directive["message"]
    assert ctx.hooks["pre_tool_call"](tool_name="culprit_timeline", args={}, task_id="t") is None


def test_slash_command_flow_with_live_progress(plugin):
    ctx, ws = plugin
    slash = ctx.commands["culprit"]
    assert "git bisect for your agent" in slash("")
    assert "No steps" not in slash("steps")
    tool_edit(ctx, ws, "calc.py", "def add(a, b):\n    return a - b\n", tool="terminal")
    tool_edit(ctx, ws, "notes.txt", "later\n")
    out = slash(CHECK)
    assert out.startswith("CULPRIT: step 1") and "/culprit revert 1" in out and "```diff" in out
    assert ctx.posted and "PASS" in ctx.posted[-1] and "FAIL" in ctx.posted[-1]
    assert "return a - b" in slash("show")
    assert "Reverted step 1 only: calc.py" in slash("revert")
    assert "passes on the current state" in slash(CHECK)
    assert "Marked step" in slash("mark")
    assert "Deleted" in slash("clear")


def test_handlers_never_raise_on_bad_input(plugin, tmp_path):
    ctx, _ws = plugin
    for tool in ctx.tools:
        for bad in (None, "x", {"step": "nope"}, {"show_step": 999}, {"test_command": ""}):
            json.loads(ctx.tools[tool](bad))
    for text in ("show 999", "show abc", "revert 999", "revert abc", "revert"):
        assert ctx.commands["culprit"](text).startswith("Culprit")
    ctx.cwd = str(tmp_path / "missing")
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={}, task_id="t") is None
    assert ctx.hooks["post_tool_call"](tool_name="terminal", args={}, result="", task_id="t") is None
    assert ctx.hooks["on_session_start"]() is None
    assert "error" in json.loads(ctx.tools["culprit_timeline"]({}))


def test_disabled_setting_pauses_recording(plugin):
    ctx, ws = plugin
    ctx.settings["enabled"] = False
    tool_edit(ctx, ws, "calc.py", "def add(a, b):\n    return 0\n")
    assert len(call(ctx, "culprit_timeline", {})["steps"]) == 1


def test_command_previews_mask_secrets():
    render = importlib.import_module(ROOT.name + ".render")
    text = render.summarize_call("terminal", {"command": "curl -H 'Authorization: Bearer abcdef123456' API_KEY=sk-123 x"})
    assert "abcdef123456" not in text and "sk-123" not in text


def test_cli_command_prints(plugin, capsys):
    ctx, _ws = plugin
    setup, handler = ctx.cli["culprit"]
    import argparse
    parser = argparse.ArgumentParser()
    setup(parser)
    handler(parser.parse_args(["steps"]))
    assert "recorded step" in capsys.readouterr().out
