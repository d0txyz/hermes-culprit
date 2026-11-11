"""Culprit engine: workspace snapshots, a step timeline, bisect and single-step revert.

Standard library only, and no Hermes imports, so the whole engine runs under plain pytest.

Snapshots live in a private git object store under the plugin's data directory. The
user's own repository (if the workspace has one) is never read from or written to: the
store only uses the workspace as a work tree for ``git add`` and ``git write-tree``.
Trees are kept without commits or refs; the store never runs gc, so they are not pruned.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_EXCLUDES = (
    # secrets: never copied into the snapshot store
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    ".ssh/", ".aws/", ".gnupg/", ".docker/", ".kube/", ".azure/", ".gcloud/",
    # dependency and build trees: large, and reproducible from the project itself
    "node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".tox/", ".mypy_cache/",
    ".pytest_cache/", ".ruff_cache/", ".next/", ".turbo/", "target/debug/", "target/release/",
)
DEFAULT_LINK_DIRS = ("node_modules", ".venv", "venv")
MAX_RANGE = 512
OUTPUT_TAIL = 1500


class CulpritError(Exception):
    """An expected, user-facing failure. The message is safe to show as is."""


def _now() -> float:
    return time.time()


# Operating-system variables a subprocess needs to start at all. Nothing else is inherited:
# provider keys, tokens and every other variable in the Hermes process stay out of git and
# out of the test command, unless the user names a variable in the ``pass_env`` setting.
_ENV_POSIX = ("PATH", "HOME", "LANG", "LANGUAGE", "TMPDIR", "TZ", "USER", "LOGNAME", "SHELL", "TERM")
_ENV_WINDOWS = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME",
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_COMMAND_CHARS = 300


def clean_env_names(names) -> list:
    """Keep only well-formed variable names from a user setting."""
    if not isinstance(names, (list, tuple)):
        return []
    return [n.strip() for n in names if isinstance(n, str) and _ENV_NAME.match(n.strip())]


def minimal_env(extra_names=()) -> dict:
    """An explicit, minimal environment: OS basics, locale, plus the names the user listed."""
    windows = os.name == "nt"
    base = _ENV_WINDOWS if windows else _ENV_POSIX
    wanted = {n.upper() if windows else n for n in base}
    wanted |= {n.upper() if windows else n for n in clean_env_names(list(extra_names))}
    env = {}
    for key, value in os.environ.items():
        probe = key.upper() if windows else key
        if probe in wanted or probe.startswith("LC_"):
            env[key] = value
    return env


def _git_env(git_dir: Path, work_tree: Optional[Path], index: Optional[Path] = None) -> dict:
    env = minimal_env()
    env["GIT_DIR"] = str(git_dir)
    if work_tree is not None:
        env["GIT_WORK_TREE"] = str(work_tree)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "culprit"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "culprit@localhost"
    return env


_GIT_FLAGS = (
    "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.fileMode=false",
    "-c", "core.longpaths=true", "-c", "gc.auto=0", "-c", "core.quotepath=false",
    "-c", "advice.addEmbeddedRepo=false", "-c", "core.hooksPath=" + os.devnull,
)


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def check_command(command: str) -> None:
    """Reject commands that could not be shown in full on one line of an approval prompt."""
    if len(command) > MAX_COMMAND_CHARS:
        raise CulpritError(
            f"The test command is longer than {MAX_COMMAND_CHARS} characters. "
            "Put it in a script file and give Culprit the command that runs the script."
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in command):
        raise CulpritError("The test command must be a single line with no control characters.")


class Engine:
    """One engine per plugin data directory. Safe to share across threads."""

    def __init__(self, data_dir: Path, excludes: Optional[list] = None,
                 link_dirs: Optional[list] = None, git: Optional[str] = None,
                 pass_env: Optional[list] = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.excludes = list(DEFAULT_EXCLUDES) + [str(x) for x in (excludes or []) if str(x).strip()]
        self.link_dirs = [str(x) for x in (link_dirs if link_dirs is not None else DEFAULT_LINK_DIRS)]
        self.pass_env = clean_env_names(pass_env or [])
        self.git = git or shutil.which("git") or ""
        self._lock = threading.RLock()
        self._disabled: dict[str, str] = {}
        self._db_path = self.data_dir / "culprit.db"
        self._init_db()

    # ------------------------------------------------------------------ storage

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS steps ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, seq INTEGER NOT NULL,"
                "kind TEXT NOT NULL, tool TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',"
                "tree TEXT NOT NULL, commit_sha TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '',"
                "ts REAL NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS steps_ws ON steps(workspace, seq)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, ts REAL NOT NULL,"
                "command TEXT NOT NULL, result TEXT NOT NULL)"
            )

    # ---------------------------------------------------------------- workspace

    @staticmethod
    def normalize(workspace: str) -> str:
        if not workspace or not str(workspace).strip():
            raise CulpritError("No workspace directory is known for this session.")
        path = Path(str(workspace)).expanduser()
        try:
            path = path.resolve()
        except OSError as exc:
            raise CulpritError(f"Cannot resolve the workspace path: {exc}") from exc
        if not path.is_dir():
            raise CulpritError(f"The workspace is not a directory: {path}")
        home = Path.home().resolve()
        if path == home or path.parent == path:
            raise CulpritError(
                f"Refusing to snapshot {path}: it is a home or root directory. "
                "Start Hermes inside a project folder."
            )
        return str(path)

    def _repo(self, workspace: str) -> Path:
        key = os.path.normcase(workspace)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        return self.data_dir / "repos" / f"{digest}.git"

    def _run_git(self, args: list, git_dir: Path, work_tree: Optional[Path] = None,
                 index: Optional[Path] = None, timeout: float = 60, cwd: Optional[Path] = None,
                 check: bool = True, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
        if not self.git:
            raise CulpritError("git was not found on PATH. Culprit needs git to take snapshots.")
        try:
            proc = subprocess.run(
                [self.git, *_GIT_FLAGS, *args], env=_git_env(git_dir, work_tree, index),
                cwd=str(cwd) if cwd else None, capture_output=True, timeout=timeout,
                input=input_bytes, creationflags=_no_window(),
                stdin=subprocess.DEVNULL if input_bytes is None else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise CulpritError(f"git {args[0]} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise CulpritError(f"Could not run git: {exc}") from exc
        if check and proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[-400:]
            raise CulpritError(f"git {args[0]} failed: {err}")
        return proc

    def _ensure_repo(self, workspace: str) -> Path:
        repo = self._repo(workspace)
        if not (repo / "HEAD").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            self._run_git(["init", "-q"], repo, Path(workspace))
        info = repo / "info"
        info.mkdir(exist_ok=True)
        wanted = "\n".join(self.excludes) + "\n"
        exclude = info / "exclude"
        if not exclude.exists() or exclude.read_text(encoding="utf-8", errors="replace") != wanted:
            exclude.write_text(wanted, encoding="utf-8")
        return repo

    # ---------------------------------------------------------------- snapshots

    def _snapshot_tree(self, workspace: str, timeout: float) -> tuple:
        repo = self._ensure_repo(workspace)
        work = Path(workspace)
        self._run_git(["add", "-A", "--", "."], repo, work, timeout=timeout, cwd=work)
        tree = self._run_git(["write-tree"], repo, work, timeout=timeout).stdout.decode().strip()
        return repo, tree

    def _last(self, conn: sqlite3.Connection, workspace: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM steps WHERE workspace=? ORDER BY seq DESC LIMIT 1", (workspace,)
        ).fetchone()

    def sync(self, workspace: str, kind: str = "external", tool: str = "", summary: str = "",
             session: str = "", timeout: float = 60) -> Optional[dict]:
        """Snapshot the workspace and record a row when its content changed.

        Returns the new row as a dict, or None when nothing changed. The first call for a
        workspace always records a ``baseline`` row.
        """
        ws = self.normalize(workspace)
        if ws in self._disabled:
            raise CulpritError(self._disabled[ws])
        with self._lock:
            try:
                repo, tree = self._snapshot_tree(ws, timeout)
            except CulpritError as exc:
                if "timed out" in str(exc):
                    self._disabled[ws] = (
                        f"Snapshots are paused for {ws}: a snapshot took longer than {timeout:.0f}s. "
                        "Add large folders to the exclude setting, then restart Hermes."
                    )
                raise
            with self._connect() as conn:
                last = self._last(conn, ws)
                if last is not None and last["tree"] == tree:
                    return None
                if last is None:
                    kind, tool, summary = "baseline", "", "state when Culprit first saw this folder"
                seq = (last["seq"] + 1) if last is not None else 0
                conn.execute(
                    "INSERT INTO steps(workspace, seq, kind, tool, summary, tree, commit_sha, session, ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (ws, seq, kind, tool, summary[:200], tree, "", session or "", _now()),
                )
                return dict(self._last(conn, ws))

    def mark(self, workspace: str, session: str = "") -> dict:
        """Record the current state as a known-good starting point for later bisects."""
        ws = self.normalize(workspace)
        with self._lock:
            self.sync(ws, kind="external", summary="changes made outside tool calls", session=session)
            with self._connect() as conn:
                last = self._last(conn, ws)
                conn.execute("UPDATE steps SET kind='mark' WHERE id=? AND kind!='baseline'", (last["id"],))
                conn.execute(
                    "INSERT INTO runs(workspace, ts, command, result) VALUES (?,?,?,?)",
                    (ws, _now(), "", json.dumps({"mark": last["seq"]})),
                )
                return dict(self._last(conn, ws))

    def _range_start(self, conn: sqlite3.Connection, ws: str) -> int:
        row = conn.execute(
            "SELECT result FROM runs WHERE workspace=? AND command='' ORDER BY id DESC LIMIT 1", (ws,)
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(json.loads(row["result"]).get("mark", 0))
        except (ValueError, TypeError):
            return 0

    def steps(self, workspace: str, since_mark: bool = True, limit: int = MAX_RANGE) -> list:
        ws = self.normalize(workspace)
        with self._lock, self._connect() as conn:
            start = self._range_start(conn, ws) if since_mark else 0
            rows = conn.execute(
                "SELECT * FROM steps WHERE workspace=? AND seq>=? ORDER BY seq DESC LIMIT ?",
                (ws, start, int(limit)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_step(self, workspace: str, seq: int) -> tuple:
        """Return (previous_row_or_None, row) for a step number."""
        ws = self.normalize(workspace)
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM steps WHERE workspace=? AND seq=?", (ws, int(seq))).fetchone()
            if row is None:
                raise CulpritError(f"There is no step {seq} in this folder. Run /culprit steps to list them.")
            prev = conn.execute("SELECT * FROM steps WHERE workspace=? AND seq=?", (ws, int(seq) - 1)).fetchone()
        return (dict(prev) if prev else None, dict(row))

    # --------------------------------------------------------------------- diff

    def diff(self, workspace: str, seq: int, max_chars: int = 6000) -> dict:
        ws = self.normalize(workspace)
        prev, row = self.get_step(ws, seq)
        if prev is None:
            return {"step": row["seq"], "stat": "", "patch": "", "files": [], "truncated": False}
        repo = self._repo(ws)
        base = ["diff", "--no-color", "--no-ext-diff", prev["tree"], row["tree"]]
        stat = self._run_git([*base[:3], "--stat=100", *base[3:]], repo).stdout.decode("utf-8", "replace").rstrip()
        names = self._run_git([*base[:3], "--name-only", *base[3:]], repo).stdout.decode("utf-8", "replace")
        patch = self._run_git(base, repo).stdout.decode("utf-8", "replace")
        truncated = len(patch) > max_chars
        if truncated:
            patch = patch[:max_chars].rstrip() + "\n... (diff truncated)"
        return {"step": row["seq"], "stat": stat, "patch": patch,
                "files": [n for n in names.splitlines() if n.strip()], "truncated": truncated}

    # ------------------------------------------------------------------- bisect

    def _materialize(self, ws: str, tree: str, dest: Path) -> None:
        repo = self._repo(ws)
        index = dest.parent / (dest.name + ".index")
        try:
            self._run_git(["read-tree", tree], repo, index=index)
            prefix = str(dest).replace("\\", "/").rstrip("/") + "/"
            self._run_git(["checkout-index", "-a", "-f", f"--prefix={prefix}"], repo, index=index, timeout=120)
        finally:
            try:
                index.unlink()
            except OSError:
                pass
        for name in self.link_dirs:
            src = Path(ws) / name
            target = dest / name
            if not src.is_dir() or target.exists():
                continue
            try:
                if os.name == "nt":
                    import _winapi
                    _winapi.CreateJunction(str(src), str(target))
                else:
                    os.symlink(str(src), str(target), target_is_directory=True)
            except (OSError, ImportError, AttributeError):
                continue

    @staticmethod
    def _cleanup(path: Path, link_dirs: list) -> None:
        # Remove links first so rmtree can never follow one into the real workspace.
        for name in link_dirs:
            link = path / name
            try:
                if os.name == "nt":
                    attrs = getattr(os.lstat(str(link)), "st_file_attributes", 0)
                    if attrs & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT: a junction or symlink
                        os.rmdir(str(link))
                elif link.is_symlink():
                    link.unlink()
            except OSError:
                pass

        def _onerror(func, target, _exc):
            try:
                os.chmod(target, 0o700)
                func(target)
            except OSError:
                pass

        if sys.version_info >= (3, 12):
            shutil.rmtree(str(path), onexc=_onerror)
        else:
            shutil.rmtree(str(path), onerror=_onerror)

    @staticmethod
    def _run_test(command: str, cwd: Path, timeout: float, pass_env=()) -> dict:
        started = time.monotonic()
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _no_window()
        else:
            kwargs["start_new_session"] = True
        env = minimal_env(pass_env)
        env["CULPRIT"] = "1"
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs,
            )
        except OSError as exc:
            return {"passed": False, "code": None, "timed_out": False, "seconds": 0.0, "output": str(exc)}
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], env=minimal_env(),
                                   capture_output=True, timeout=15, creationflags=_no_window())
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                proc.kill()
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.SubprocessError:
                out = b""
        text = (out or b"").decode("utf-8", "replace")
        return {
            "passed": (proc.returncode == 0) and not timed_out, "code": proc.returncode,
            "timed_out": timed_out, "seconds": round(time.monotonic() - started, 2),
            "output": text[-OUTPUT_TAIL:],
        }

    def bisect(self, workspace: str, command: str, timeout: float = 300, session: str = "",
               progress: Optional[Callable[[dict], None]] = None, since_mark: bool = True) -> dict:
        """Find the first recorded step after which ``command`` stops exiting 0.

        Every probe runs in a fresh temporary copy of that step's snapshot. The live
        workspace is never modified.
        """
        ws = self.normalize(workspace)
        command = (command or "").strip()
        if not command:
            raise CulpritError("Give a test command, for example: /culprit pytest -q")
        check_command(command)
        started = time.monotonic()
        with self._lock:
            self.sync(ws, kind="external", summary="changes made outside tool calls", session=session)
            states = self.steps(ws, since_mark=since_mark)
        result: dict[str, Any] = {
            "command": command, "workspace": ws, "steps_in_range": len(states), "probes": [],
            "status": "", "culprit": None,
        }
        if len(states) < 2:
            result["status"] = "no_history"
            result["message"] = (
                "Culprit has recorded no changes in this folder yet, so there is nothing to search. "
                "It records a step each time a tool call changes a file."
            )
            return self._finish(ws, result, started)

        cache: dict[int, dict] = {}
        tmp_root = Path(tempfile.mkdtemp(prefix="culprit-"))

        def probe(i: int) -> bool:
            if i in cache:
                return cache[i]["passed"]
            row = states[i]
            dest = tmp_root / f"step-{row['seq']}"
            dest.mkdir()
            try:
                self._materialize(ws, row["tree"], dest)
                outcome = self._run_test(command, dest, timeout, self.pass_env)
                # Show paths relative to the project, not the throwaway copy.
                for form in (str(dest), str(dest).replace("\\", "/"), str(dest).replace("\\", "\\\\")):
                    outcome["output"] = outcome["output"].replace(form, ".")
            finally:
                self._cleanup(dest, self.link_dirs)
            entry = {"step": row["seq"], "tool": row["tool"], "summary": row["summary"],
                     "kind": row["kind"], **outcome}
            cache[i] = entry
            result["probes"].append(entry)
            if progress is not None:
                try:
                    progress(entry)
                except Exception:
                    pass
            return outcome["passed"]

        try:
            last = len(states) - 1
            if probe(last):
                result["status"] = "passing"
                result["message"] = "The command passes on the current state. Nothing is broken."
                return self._finish(ws, result, started)
            if not probe(0):
                result["status"] = "failing_at_start"
                result["message"] = (
                    f"The command already fails at step {states[0]['seq']}, the start of the range, "
                    "so no recorded step broke it. If it has a longer history, try: /culprit all <command>"
                )
                return self._finish(ws, result, started)
            good, bad = 0, last
            while bad - good > 1:
                mid = (good + bad) // 2
                if probe(mid):
                    good = mid
                else:
                    bad = mid
            culprit = states[bad]
            result["status"] = "found"
            result["culprit"] = {
                "step": culprit["seq"], "kind": culprit["kind"], "tool": culprit["tool"],
                "summary": culprit["summary"], "ts": culprit["ts"], "session": culprit["session"],
                "last_good_step": states[good]["seq"],
            }
            result["diff"] = self.diff(ws, culprit["seq"])
            result["failure_output"] = cache[bad]["output"]
            return self._finish(ws, result, started)
        finally:
            shutil.rmtree(str(tmp_root), ignore_errors=True)

    def _finish(self, ws: str, result: dict, started: float) -> dict:
        result["seconds"] = round(time.monotonic() - started, 2)
        result["test_runs"] = len(result["probes"])
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runs(workspace, ts, command, result) VALUES (?,?,?,?)",
                (ws, _now(), result["command"], json.dumps(result)),
            )
        return result

    def last_run(self, workspace: str) -> Optional[dict]:
        ws = self.normalize(workspace)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT ts, result FROM runs WHERE workspace=? AND command!='' ORDER BY id DESC LIMIT 1", (ws,)
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["result"])
        data["ts"] = row["ts"]
        return data

    # ------------------------------------------------------------------- revert

    def revert(self, workspace: str, seq: Optional[int] = None, session: str = "") -> dict:
        """Undo exactly one step in the live workspace, keeping every later change.

        All or nothing: if the reverse patch does not apply cleanly, no file is touched.
        """
        ws = self.normalize(workspace)
        with self._lock:
            if seq is None:
                run = self.last_run(ws)
                if not run or not run.get("culprit"):
                    raise CulpritError("No culprit has been found yet. Run /culprit <test command> first.")
                seq = int(run["culprit"]["step"])
            prev, row = self.get_step(ws, seq)
            if prev is None:
                raise CulpritError("The first recorded state has nothing before it, so it cannot be reverted.")
            self.sync(ws, kind="external", summary="changes made outside tool calls", session=session)
            repo = self._repo(ws)
            patch = self._run_git(
                ["diff", "--binary", "--full-index", "--no-color", "--no-ext-diff", prev["tree"], row["tree"]],
                repo,
            ).stdout
            if not patch.strip():
                raise CulpritError(f"Step {seq} did not change any file.")
            files = self.diff(ws, seq)["files"]
            handle, name = tempfile.mkstemp(prefix="culprit-", suffix=".patch")
            try:
                with os.fdopen(handle, "wb") as fh:
                    fh.write(patch)
                args = ["apply", "-R", "--whitespace=nowarn"]
                check = self._run_git([*args, "--check", name], repo, Path(ws), cwd=Path(ws), check=False)
                if check.returncode != 0:
                    detail = check.stderr.decode("utf-8", "replace").strip()[-600:]
                    raise CulpritError(
                        f"Step {seq} cannot be reverted cleanly because later changes touch the same lines. "
                        "No file was changed.\n" + detail
                    )
                done = self._run_git([*args, name], repo, Path(ws), cwd=Path(ws), check=False)
                if done.returncode != 0:
                    detail = done.stderr.decode("utf-8", "replace").strip()[-600:]
                    raise CulpritError(f"Reverting step {seq} failed: {detail}")
            finally:
                try:
                    os.unlink(name)
                except OSError:
                    pass
            new = self.sync(ws, kind="revert", tool="culprit", summary=f"reverted step {seq}", session=session)
            return {"reverted_step": seq, "files": files, "recorded_as_step": new["seq"] if new else None}

    # -------------------------------------------------------------------- clear

    def clear(self, workspace: str) -> dict:
        """Delete every snapshot and record Culprit holds for this workspace."""
        ws = self.normalize(workspace)
        with self._lock:
            with self._connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM steps WHERE workspace=?", (ws,)).fetchone()[0]
                conn.execute("DELETE FROM steps WHERE workspace=?", (ws,))
                conn.execute("DELETE FROM runs WHERE workspace=?", (ws,))
            repo = self._repo(ws)
            if repo.exists():
                self._cleanup(repo, [])
            self._disabled.pop(ws, None)
        return {"deleted_steps": count}
