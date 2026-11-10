"""Plain-text rendering for slash command and CLI output."""

from __future__ import annotations

import re
import time

_SECRET = re.compile(
    r"(?i)\b([a-z0-9_]*(?:api[_-]?key|token|secret|password|passwd|authorization)[a-z0-9_]*)(\s*[=:]\s*)(\S+)"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[a-z0-9._\-]{8,}")

HELP = """Culprit: git bisect for your agent.

  /culprit <test command>   find the step that made the command start failing
  /culprit all <command>    same, but search the whole history, not just since the last mark
  /culprit steps            list the recorded steps in this folder
  /culprit show [step]      show what one step changed (default: the last culprit)
  /culprit revert [step]    undo only that step, keeping every later change
  /culprit mark             call the current state known-good; later searches start here
  /culprit clear            delete every snapshot Culprit holds for this folder
  /culprit help             this text

A step is recorded each time a tool call changes a file. The test command runs in
temporary copies of each step, so your working folder is not touched while searching."""


def mask(text: str) -> str:
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text or "")
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def summarize_call(tool_name: str, args) -> str:
    """A short, masked label for a tool call: a path, or the start of a command."""
    if not isinstance(args, dict):
        return ""
    for key in ("path", "file_path", "filename", "target", "command", "code", "url", "query"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().splitlines()[0]
            return mask(first)[:100]
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return mask(value.strip().splitlines()[0])[:100]
    return ""


def label(row: dict) -> str:
    kind = row.get("kind", "")
    if kind == "baseline":
        return "(start) first state Culprit saw"
    if kind == "external":
        return "(you) edits made outside tool calls"
    if kind == "mark":
        return "(mark) " + (f"{row.get('tool')}: {row.get('summary')}" if row.get("tool") else "known-good point")
    tool = row.get("tool") or "tool"
    summary = row.get("summary") or ""
    return f"{tool}: {summary}" if summary else tool


def _clock(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def render_steps(rows: list, limit: int = 40) -> str:
    if not rows:
        return "No steps recorded in this folder yet. Culprit records one each time a tool call changes a file."
    shown = rows[-limit:]
    lines = [f"{len(rows)} recorded step(s) in range" + (f", showing the last {len(shown)}" if len(shown) < len(rows) else "") + ":"]
    for row in shown:
        lines.append(f"  step {row['seq']:>3}  {_clock(row['ts'])}  {label(row)}")
    lines.append("")
    lines.append("Find what broke: /culprit <test command>")
    return "\n".join(lines)


def render_probe(entry: dict) -> str:
    verdict = "PASS" if entry.get("passed") else ("TIME" if entry.get("timed_out") else "FAIL")
    return f"  step {entry['step']:>3}  {verdict}  {entry.get('seconds', 0):>6.1f}s  {label(entry)}"


def render_diff(diff: dict) -> str:
    if not diff or not (diff.get("stat") or diff.get("patch")):
        return "(no file changes)"
    parts = []
    if diff.get("stat"):
        parts.append(diff["stat"])
    if diff.get("patch"):
        parts.append("```diff\n" + diff["patch"].rstrip() + "\n```")
    return "\n".join(parts)


def render_result(result: dict) -> str:
    status = result.get("status")
    lines = []
    if status == "found":
        c = result["culprit"]
        lines.append(f"CULPRIT: step {c['step']}  {label(c)}")
        lines.append(
            f"Searched {result['steps_in_range']} states with {result['test_runs']} test runs in "
            f"{result['seconds']:.1f}s. Last good: step {c['last_good_step']}."
        )
    else:
        lines.append(result.get("message", "No result."))
    if result.get("probes"):
        lines.append("")
        lines.append(f"Test runs of `{result['command']}`:")
        lines.extend(render_probe(p) for p in result["probes"])
    if status == "found":
        lines.append("")
        lines.append("What that step changed:")
        lines.append(render_diff(result.get("diff", {})))
        tail = (result.get("failure_output") or "").strip()
        if tail:
            lines.append("")
            lines.append("Failure output at that step (tail):")
            lines.append("```\n" + "\n".join(tail.splitlines()[-12:]) + "\n```")
        lines.append("")
        lines.append(f"Undo only this step: /culprit revert {result['culprit']['step']}")
        if result["culprit"].get("kind") == "external":
            lines.append("Note: this change was not made by a tool call. It happened between tool calls.")
    return "\n".join(lines)
