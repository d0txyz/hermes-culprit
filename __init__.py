"""Culprit: git bisect for your agent.

Records a snapshot each time a tool call changes a file, then binary-searches those
snapshots with a test command to name the exact step that broke it, and can undo only
that step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

from . import render
from .engine import CulpritError, Engine, check_command

logger = logging.getLogger(__name__)

PLUGIN = "culprit"

# Tools that cannot change files in the workspace: no snapshot is taken after them.
_READ_ONLY = {
    "read_file", "search_files", "list_files", "skill_view", "skills_list", "todo", "memory",
    "session_search", "clarify", "vision_analyze", "web_search", "web_extract", "tool_search",
    "tool_describe", "culprit_timeline", "culprit_bisect", "culprit_revert",
}
_READ_ONLY_PREFIXES = ("web_", "browser_", "skill", "memory_", "image_", "tts_")
_GATED = {"culprit_bisect", "culprit_revert"}
# Surfaces where the person typing is the owner at their own machine.
_LOCAL_SURFACES = {"cli", "tui", "desktop", "acp"}
# Slash subcommands that only read: open on every surface.
_READ_ONLY_WORDS = {"", "help", "-h", "--help", "steps", "status", "list", "log", "show"}
# After a snapshot, skip the cheap pre-call check for this long: bursts of tool calls
# leave no room for a person to edit in between.
_PRE_SYNC_QUIET_SECONDS = 3.0
_PRE_SYNC_TIMEOUT = 8.0

TIMELINE_SCHEMA = {
    "name": "culprit_timeline",
    "description": (
        "List the file-changing steps Culprit recorded in the current project folder: one row per "
        "tool call (or outside edit) that changed files, with its step number. Read-only. Use it to "
        "see what changed and in what order before calling culprit_bisect, or to show one step's diff."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "show_step": {
                "type": "integer",
                "description": "Optional step number. When given, also return that step's diff.",
            },
            "limit": {"type": "integer", "description": "Most recent steps to return (default 40)."},
        },
        "required": [],
    },
}
BISECT_SCHEMA = {
    "name": "culprit_bisect",
    "description": (
        "Find the exact step that broke something. Give a shell command that exits 0 when things work "
        "and non-zero when they are broken (a test, a build, a script). Culprit binary-searches its "
        "recorded snapshots, running the command in temporary copies so the working folder is not "
        "modified, and returns the first step after which the command fails, with that step's diff. "
        "Use this when something worked earlier in the session and fails now and the cause is not "
        "obvious. Asks the user for approval before running."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "test_command": {
                "type": "string",
                "description": "Command that exits 0 when good, e.g. 'pytest -q tests/test_api.py' or 'npm run build'.",
            },
            "whole_history": {
                "type": "boolean",
                "description": "Search every recorded step instead of only those since the last mark (default false).",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Time limit for one run of the command (default: the plugin setting, 300).",
            },
        },
        "required": ["test_command"],
    },
}
REVERT_SCHEMA = {
    "name": "culprit_revert",
    "description": (
        "Undo exactly one recorded step in the working folder and keep every later change. All or "
        "nothing: if later edits touch the same lines, nothing is changed and the conflict is reported. "
        "Defaults to the step the last culprit_bisect found. Asks the user for approval first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "step": {"type": "integer", "description": "Step number to undo. Default: the last culprit found."},
        },
        "required": [],
    },
}


class _Plugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self._engine = None
        self._engine_key = None
        self._lock = threading.Lock()
        self._last_sync: dict[str, float] = {}
        self._pre_off: set[str] = set()

    # ------------------------------------------------------------- environment

    def _setting(self, key: str, default):
        try:
            value = self.ctx.get_config(key, default=default)
        except Exception:
            return default
        return default if value is None else value

    def _data_dir(self) -> Path:
        try:
            from plugins.plugin_storage import plugin_data_dir
            return Path(plugin_data_dir(PLUGIN))
        except Exception:
            home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
            path = Path(home) / "plugin-data" / PLUGIN
            path.mkdir(parents=True, exist_ok=True)
            return path

    def engine(self) -> Engine:
        data_dir = self._data_dir()
        excludes = self._setting("exclude", [])
        links = self._setting("link_dirs", None)
        pass_env = self._setting("pass_env", [])
        key = (str(data_dir), json.dumps(excludes, default=str), json.dumps(links, default=str),
               json.dumps(pass_env, default=str))
        with self._lock:
            if self._engine is None or self._engine_key != key:
                self._engine = Engine(
                    data_dir,
                    excludes=excludes if isinstance(excludes, list) else [],
                    link_dirs=links if isinstance(links, list) else None,
                    pass_env=pass_env if isinstance(pass_env, list) else [],
                )
                self._engine_key = key
            return self._engine

    def workspace(self) -> str:
        getter = getattr(self.ctx, "session_cwd", None)
        if callable(getter):
            try:
                cwd = getter()
                if cwd:
                    return str(cwd)
            except Exception:
                pass
        return os.getcwd()

    def surface(self) -> str:
        """The calling session's surface, or "" when Hermes cannot say."""
        getter = getattr(self.ctx, "session_surface", None)
        if callable(getter):
            try:
                return str(getter() or "").strip().lower()
            except Exception:
                return ""
        return ""

    def _revert_step(self, args):
        """The step a revert call targets: the one given, else the last culprit found."""
        step = args.get("step") if isinstance(args, dict) else None
        if step is not None:
            return int(step)
        run = self.engine().last_run(self.workspace())
        if run and run.get("culprit"):
            return int(run["culprit"]["step"])
        return None

    def _approval(self, tool_name: str, args) -> dict:
        """The approval directive for a model-initiated call: it shows exactly what will run."""
        args = args if isinstance(args, dict) else {}
        try:
            folder = self.workspace()
        except Exception:
            folder = "this folder"
        if tool_name == "culprit_bisect":
            command = str(args.get("test_command") or "").strip()
            try:
                check_command(command)
            except CulpritError as exc:
                # The handler rejects this command without running anything.
                return {"action": "approve", "rule_key": "culprit:bisect:rejected",
                        "message": f"Culprit will refuse this test command and run nothing: {exc}"}
            digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
            return {
                "action": "approve", "rule_key": f"culprit:bisect:{digest}",
                "message": ("Culprit wants to run this shell command several times, in temporary copies of "
                            f"{folder}: {render.mask(command)}"),
            }
        try:
            step = self._revert_step(args)
        except (CulpritError, TypeError, ValueError):
            step = None
        if step is None:
            return {"action": "approve", "rule_key": "culprit:revert:none",
                    "message": "Culprit was asked to undo a step, but no valid step is named. Nothing will change."}
        what = ""
        try:
            _prev, row = self.engine().get_step(folder, step)
            what = f" ({render.label(row)})"
        except Exception:
            pass
        return {
            "action": "approve", "rule_key": f"culprit:revert:{step}",
            "message": f"Culprit wants to undo step {step}{what} in {folder}, keeping later changes.",
        }

    def _timeout(self, override=None) -> float:
        try:
            value = float(override if override else self._setting("test_timeout", 300))
        except (TypeError, ValueError):
            value = 300.0
        return max(1.0, min(value, 3600.0))

    # -------------------------------------------------------------------- hooks

    def on_session_start(self, **kwargs):
        if not self._setting("enabled", True):
            return None
        try:
            ws = self.workspace()
            self.engine().sync(ws, kind="external", summary="changes made outside tool calls",
                               session=str(kwargs.get("session_id") or ""), timeout=_PRE_SYNC_TIMEOUT)
            self._last_sync[ws] = time.monotonic()
        except CulpritError as exc:
            logger.debug("culprit: session start snapshot skipped: %s", exc)
        except Exception:
            logger.debug("culprit: session start snapshot failed", exc_info=True)
        return None

    def pre_tool_call(self, tool_name: str = "", args=None, **kwargs):
        try:
            if tool_name in _GATED:
                return self._approval(tool_name, args)
            if not self._setting("enabled", True) or self._skip(tool_name):
                return None
            ws = self.workspace()
            if ws in self._pre_off:
                return None
            if time.monotonic() - self._last_sync.get(ws, 0.0) < _PRE_SYNC_QUIET_SECONDS:
                return None
            started = time.monotonic()
            try:
                # Anything that changed before this tool runs was not this tool's doing.
                self.engine().sync(ws, kind="external", summary="changes made outside tool calls",
                                   session=str(kwargs.get("session_id") or ""), timeout=_PRE_SYNC_TIMEOUT)
            except CulpritError as exc:
                self._pre_off.add(ws)
                logger.debug("culprit: pre-call snapshot off for %s: %s", ws, exc)
            if time.monotonic() - started > _PRE_SYNC_TIMEOUT:
                self._pre_off.add(ws)
            self._last_sync[ws] = time.monotonic()
        except Exception:
            logger.debug("culprit: pre_tool_call failed", exc_info=True)
        return None

    def post_tool_call(self, tool_name: str = "", args=None, **kwargs):
        try:
            if not self._setting("enabled", True) or self._skip(tool_name):
                return None
            ws = self.workspace()
            self.engine().sync(ws, kind="step", tool=str(tool_name),
                               summary=render.summarize_call(tool_name, args),
                               session=str(kwargs.get("session_id") or ""))
            self._last_sync[ws] = time.monotonic()
        except CulpritError as exc:
            logger.debug("culprit: snapshot skipped: %s", exc)
        except Exception:
            logger.debug("culprit: post_tool_call failed", exc_info=True)
        return None

    @staticmethod
    def _skip(tool_name: str) -> bool:
        name = str(tool_name or "")
        return (not name) or name in _READ_ONLY or name.startswith(_READ_ONLY_PREFIXES)

    # -------------------------------------------------------------------- tools

    def tool_timeline(self, args: dict, **kwargs) -> str:
        try:
            args = args if isinstance(args, dict) else {}
            ws = self.workspace()
            engine = self.engine()
            rows = engine.steps(ws, since_mark=False)
            limit = max(1, min(int(args.get("limit") or 40), 200))
            out = {
                "workspace": ws, "total_steps": len(rows),
                "steps": [
                    {"step": r["seq"], "kind": r["kind"], "tool": r["tool"], "summary": r["summary"],
                     "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))}
                    for r in rows[-limit:]
                ],
            }
            if args.get("show_step") is not None:
                out["diff"] = engine.diff(ws, int(args["show_step"]))
            last = engine.last_run(ws)
            if last and last.get("culprit"):
                out["last_culprit"] = {"step": last["culprit"]["step"], "command": last["command"]}
            return json.dumps(out)
        except CulpritError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            logger.debug("culprit_timeline failed", exc_info=True)
            return json.dumps({"error": f"culprit_timeline failed: {exc}"})

    def tool_bisect(self, args: dict, **kwargs) -> str:
        try:
            args = args if isinstance(args, dict) else {}
            ws = self.workspace()
            result = self.engine().bisect(
                ws, str(args.get("test_command") or ""), timeout=self._timeout(args.get("timeout_seconds")),
                session=str(kwargs.get("session_id") or ""), since_mark=not bool(args.get("whole_history")),
            )
            slim = {k: v for k, v in result.items() if k != "probes"}
            slim["probes"] = [
                {"step": p["step"], "passed": p["passed"], "timed_out": p["timed_out"], "seconds": p["seconds"]}
                for p in result["probes"]
            ]
            if result.get("status") == "found":
                slim["next"] = (
                    f"Explain the diff to the user. To undo only step {result['culprit']['step']}, "
                    "call culprit_revert, or fix the code directly."
                )
            return json.dumps(slim)
        except CulpritError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            logger.debug("culprit_bisect failed", exc_info=True)
            return json.dumps({"error": f"culprit_bisect failed: {exc}"})

    def tool_revert(self, args: dict, **kwargs) -> str:
        try:
            args = args if isinstance(args, dict) else {}
            step = self._revert_step(args)
            if step is None:
                raise CulpritError("No culprit has been found yet. Run culprit_bisect first, or give a step.")
            done = self.engine().revert(self.workspace(), step,
                                        session=str(kwargs.get("session_id") or ""))
            return json.dumps({"ok": True, **done})
        except CulpritError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.debug("culprit_revert failed", exc_info=True)
            return json.dumps({"ok": False, "error": f"culprit_revert failed: {exc}"})

    # ------------------------------------------------------------ slash and CLI

    def command(self, raw_args: str = "", announce=None, from_chat: bool = False) -> str:
        """Handle `/culprit ...` and `hermes culprit ...`. Always returns text, never raises."""
        try:
            raw = str(raw_args or "").strip()
            if from_chat:
                refusal = self._chat_refusal(raw)
                if refusal:
                    return refusal
            return self._command(raw, announce)
        except CulpritError as exc:
            return f"Culprit: {exc}"
        except Exception as exc:
            logger.debug("culprit command failed", exc_info=True)
            return f"Culprit hit an unexpected error: {exc}"

    def _chat_refusal(self, raw: str) -> str:
        """Outside the owner's own CLI or Desktop, /culprit may only read.

        Running a test command is host shell, and revert, mark and clear change files or
        history. On a messaging platform any admitted sender could type them, so they are
        refused there unless the owner turns on the allow_remote_commands setting.
        """
        word, _, rest = raw.partition(" ")
        word, rest = word.lower(), rest.strip()
        if word in _READ_ONLY_WORDS and (word == "show" or not rest):
            return ""
        surface = self.surface()
        if surface in _LOCAL_SURFACES:
            return ""
        if self._setting("allow_remote_commands", False) is True:
            return ""
        where = f"on {surface}" if surface else "here"
        return (
            f"Culprit: this command is not available {where}. Running a test command, revert, mark and "
            "clear only work from the Hermes CLI or Desktop app on the machine itself, because they run "
            "shell commands or change files. You can still use /culprit steps, /culprit show [step] and "
            "/culprit help. The owner can allow this with the allow_remote_commands setting."
        )

    def _command(self, raw: str, announce) -> str:
        word, _, rest = raw.partition(" ")
        word, rest = word.lower(), rest.strip()
        engine = self.engine()
        ws = self.workspace()
        if raw == "" or word in {"help", "-h", "--help"}:
            return render.HELP
        if word in {"steps", "status", "list", "log"} and not rest:
            # Pick up edits made since the last tool call, so the list matches the folder.
            engine.sync(ws, kind="external", summary="changes made outside tool calls")
            return render.render_steps(engine.steps(ws, since_mark=False))
        if word == "show":
            step = self._step_arg(rest, engine, ws)
            _prev, row = engine.get_step(ws, step)
            return f"step {row['seq']}  {render.label(row)}\n" + render.render_diff(engine.diff(ws, step))
        if word in {"revert", "undo"}:
            done = engine.revert(ws, int(rest) if rest else None)
            files = ", ".join(done["files"]) or "(no files)"
            return (f"Reverted step {done['reverted_step']} only: {files}\n"
                    f"Later changes were kept. The revert is recorded as step {done['recorded_as_step']}, "
                    "so it can be undone the same way.")
        if word == "mark" and not rest:
            row = engine.mark(ws)
            return f"Marked step {row['seq']} as known-good. Later searches start from here."
        if word == "clear" and not rest:
            done = engine.clear(ws)
            return f"Deleted {done['deleted_steps']} recorded step(s) and their snapshots for {ws}."
        whole = False
        command = raw
        if word in {"all", "run"} and rest:
            whole, command = (word == "all"), rest
        lines: list[str] = []

        def progress(entry: dict) -> None:
            lines.append(render.render_probe(entry))
            if announce is not None:
                announce("Culprit is searching...\n" + "\n".join(lines))

        result = engine.bisect(ws, command, timeout=self._timeout(), progress=progress, since_mark=not whole)
        return render.render_result(result)

    @staticmethod
    def _step_arg(rest: str, engine: Engine, ws: str) -> int:
        if rest:
            try:
                return int(rest)
            except ValueError:
                raise CulpritError(f"'{rest}' is not a step number. Run /culprit steps to list them.") from None
        run = engine.last_run(ws)
        if not run or not run.get("culprit"):
            raise CulpritError("No culprit has been found yet. Give a step number, or run /culprit <test command>.")
        return int(run["culprit"]["step"])

    def slash(self, raw_args: str = "") -> str:
        handle = {"msg": None, "off": False}

        def announce(text: str) -> None:
            # Live progress where the surface supports it; silently absent elsewhere.
            if handle["off"]:
                return
            try:
                if handle["msg"] is None:
                    poster = getattr(self.ctx, "post_message", None)
                    msg = poster(text) if callable(poster) else None
                    if msg is None or getattr(msg, "ok", True) is False or not hasattr(msg, "edit"):
                        handle["off"] = True
                        return
                    handle["msg"] = msg
                else:
                    handle["msg"].edit(text)
            except Exception:
                handle["off"] = True

        return self.command(raw_args, announce, from_chat=True)


def _cli_setup(parser) -> None:
    import argparse
    parser.add_argument("words", nargs=argparse.REMAINDER,
                        help="steps | show [n] | revert [n] | mark | clear | <test command>")


def register(ctx) -> None:
    plugin = _Plugin(ctx)

    ctx.register_hook("on_session_start", plugin.on_session_start)
    ctx.register_hook("pre_tool_call", plugin.pre_tool_call)
    ctx.register_hook("post_tool_call", plugin.post_tool_call)

    ctx.register_tool(name="culprit_timeline", toolset=PLUGIN, schema=TIMELINE_SCHEMA, handler=plugin.tool_timeline)
    ctx.register_tool(name="culprit_bisect", toolset=PLUGIN, schema=BISECT_SCHEMA, handler=plugin.tool_bisect)
    ctx.register_tool(name="culprit_revert", toolset=PLUGIN, schema=REVERT_SCHEMA, handler=plugin.tool_revert)

    ctx.register_command(
        "culprit", plugin.slash,
        description="Find the exact step that broke your tests, and undo only that step",
        args_hint="<test command> | steps | show [n] | revert [n] | mark | clear",
    )

    def _cli(namespace) -> None:
        words = [w for w in (getattr(namespace, "words", None) or []) if w != "--"]
        joined = subprocess.list2cmdline(words) if os.name == "nt" else shlex.join(words)
        print(plugin.command(joined))

    ctx.register_cli_command(
        "culprit", "Find the step that broke your tests (git bisect for agent sessions)",
        _cli_setup, _cli,
        description="Search Culprit's recorded snapshots of the current folder with a test command.",
    )
