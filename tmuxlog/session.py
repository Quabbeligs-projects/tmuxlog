from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from tmuxlog import paths

NEW_PANE_HOOKS = ("after-split-window", "after-new-window")

_PANE_FMT = "\t".join("#{" + f + "}" for f in (
    "pane_id", "pane_pid", "session_name", "window_index", "window_name",
    "pane_index", "pane_title", "pane_tty", "pane_pipe"))

class Tmux:
    """Wrapper over tmux binary + optional socket."""

    def __init__(self, base=("tmux",)):
        self.base = list(base)

    def run(self, *args, check=True):
        return subprocess.run(self.base + list(args), capture_output=True, text=True, check=check)

    def has_session(self, name: str) -> bool:
        return self.run("has-session", "-t", name, check=False).returncode == 0

    def server_pid(self) -> int:
        return int(self.run("display-message", "-p", "#{pid}").stdout.strip())

    def new_session_detached(self, name: str) -> None:
        self.run("new-session", "-d", "-s", name)

    def list_panes(self, name: str) -> list[dict]:
        out = self.run("list-panes", "-s", "-t", name, "-F", _PANE_FMT).stdout
        panes = []
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) < 9:
                continue
            panes.append({
                "pane_id": f[0], "pane_pid": f[1], "session": f[2],
                "window_index": f[3], "window_name": f[4], "pane_index": f[5],
                "pane_title": f[6], "tty": f[7], "piped": f[8] == "1",
            })
        return panes

    def pipe_pane(self, pane_id: str, cmd: str) -> None:
        self.run("pipe-pane", "-O", "-t", pane_id, cmd)

    def pipe_pane_off(self, pane_id: str) -> None:
        if self.run("display-message", "-p", "-t", pane_id, "#{pane_pipe}",
                    check=False).stdout.strip() == "1":
            self.run("pipe-pane", "-t", pane_id, check=False)

    def set_hook(self, name: str, hook: str, cmd: str) -> None:
        self.run("set-hook", "-t", name, hook, cmd)

    def unset_hook(self, name: str, hook: str) -> None:
        self.run("set-hook", "-u", "-t", name, hook, check=False)

PKG_ROOT = Path(__file__).resolve().parent.parent

def _launcher(cfg, exec_: bool = False) -> str:
    """Shell command prefix that runs tmuxlog from inside tmux (pipe-pane, run-shell).

    tmux runs these with the *server's* environment and cwd, so nothing may
    depend on the caller's PYTHONPATH, cwd or TMUXLOG_ROOT. The state root is
    pinned explicitly. By default this interpreter runs the package from where
    it actually lives (works for bin/tmuxlog in a checkout and for an install);
    `[capture] cmd` overrides the launcher, e.g. "/usr/local/bin/tmuxlog".
    With exec_, the shell is replaced (env assignments must precede `exec`).
    """
    root = shlex.quote(str(Path((cfg or {}).get("paths", {}).get("root", "~/.tmuxlog")).expanduser()))
    env = [f"TMUXLOG_ROOT={root}"]
    override = ((cfg or {}).get("capture", {}).get("cmd") or "").strip()
    if override:
        if override.endswith(" capture"):
            override = override[: -len(" capture")]
        cmd = override
    else:
        env.append(f"PYTHONPATH={shlex.quote(str(PKG_ROOT))}")
        cmd = f"{shlex.quote(sys.executable)} -m tmuxlog"
    return " ".join(env + (["exec"] if exec_ else []) + [cmd])

def _capture_cmd(cfg) -> str:
    """Command pipe-pane runs per pane (exec: the capture process is the pipe's child)."""
    return f"{_launcher(cfg, exec_=True)} capture"

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def _owns_pipe(pane_dir: Path) -> bool:
    """True if a capture we started is still running for this pane."""
    f = pane_dir / "capture.pid"
    if not f.exists():
        return False
    try:
        pid = int(f.read_text().split()[0])
    except (ValueError, IndexError, OSError):
        return False
    return _pid_alive(pid)

def _run_dir(root: Path, server_pid: int) -> Path:
    return paths.ensure_dir(root / "run" / str(server_pid))

def arm_pane(pane: dict, session_dir: Path, root: Path, server_pid: int,
             boundary: str, tmux: Tmux, cfg=None) -> dict:
    """Create the pane's files and start (or skip) its capture pipe. Idempotent."""
    pane_num = pane["pane_id"].lstrip("%")
    ptr = _run_dir(root, server_pid) / pane_num

    if ptr.exists():
        pane_dir = Path(ptr.read_text().strip())
    else:
        uid = f"{pane_num}-{server_pid}-{time.time_ns()}"
        pane_dir = session_dir / "panes" / uid
    paths.ensure_dir(pane_dir)

    meta = {
        "meta_version": 1,
        "pane_uid": pane_dir.name,
        "pane_id": pane["pane_id"],
        "pane_num": int(pane_num),
        "session": pane["session"],
        "window_index": int(pane["window_index"]),
        "window_name": pane["window_name"],
        "pane_index": int(pane["pane_index"]),
        "pane_title": pane["pane_title"],
        "pane_pid": int(pane["pane_pid"]),
        "tty": pane["tty"],
        "server_pid": server_pid,
        "created_ns": time.time_ns(),
        "boundary": boundary,
    }
    if not (pane_dir / "meta.json").exists():
        paths.write_private_atomic(pane_dir / "meta.json", (json.dumps(meta) + "\n").encode())
    paths.open_private(pane_dir / "hooks.rec", "ab").__enter__().close()
    paths.write_private_atomic(pane_dir / "boundary", (boundary + "\n").encode())
    paths.write_private_atomic(ptr, (str(pane_dir) + "\n").encode())

    status = "armed"
    if pane["piped"]:
        if _owns_pipe(pane_dir):
            status = "already-ours"
        else:
            status = "foreign-pipe"
    else:
        cmd = f"{_capture_cmd(cfg)} --pane-dir {shlex.quote(str(pane_dir))}"
        tmux.pipe_pane(pane["pane_id"], cmd)
    return {"pane_id": pane["pane_id"], "pane_dir": str(pane_dir), "status": status}

def start_session(name: str | None, cfg: dict, tmux: Tmux | None = None,
                  boundary: str = "osc") -> dict:
    tmux = tmux or Tmux()
    root = Path(cfg["paths"]["root"])

    if not name:
        name = os.environ.get("TMUX") and _current_session(tmux) or None
    if not name:
        raise SystemExit("tmuxlog: no session name and not inside tmux")
    created = False
    if not tmux.has_session(name):
        tmux.new_session_detached(name)
        created = True

    server_pid = tmux.server_pid()
    session_dir = paths.ensure_dir(root / "sessions" / name)

    results = [arm_pane(p, session_dir, root, server_pid, boundary, tmux, cfg)
               for p in tmux.list_panes(name)]

    self_cmd = f"{_launcher(cfg)} arm-new --session {shlex.quote(name)}"
    for hook in NEW_PANE_HOOKS:
        tmux.set_hook(name, hook, f"run-shell {shlex.quote(self_cmd)}")

    session_json = {
        "name": name, "server_pid": server_pid, "created_ns": time.time_ns(),
        "boundary": boundary, "hooks": list(NEW_PANE_HOOKS), "created_session": created,
    }
    paths.write_private_atomic(session_dir / "session.json",
                               (json.dumps(session_json) + "\n").encode())
    return {"session": name, "dir": str(session_dir), "created": created,
            "boundary": boundary, "panes": results}

def arm_new(name: str, cfg: dict, tmux: Tmux | None = None) -> dict:
    """Arm any panes in the session that aren't armed yet (called by the hooks)."""
    tmux = tmux or Tmux()
    root = Path(cfg["paths"]["root"])
    session_dir = root / "sessions" / name
    boundary = "osc"
    sj = session_dir / "session.json"
    if sj.exists():
        boundary = json.loads(sj.read_text()).get("boundary", "osc")
    if not tmux.has_session(name):
        return {"session": name, "panes": []}
    server_pid = tmux.server_pid()
    run_dir = root / "run" / str(server_pid)
    out = []
    for p in tmux.list_panes(name):
        if (run_dir / p["pane_id"].lstrip("%")).exists() and p["piped"]:
            continue
        out.append(arm_pane(p, session_dir, root, server_pid, boundary, tmux, cfg))
    return {"session": name, "panes": out}

def stop_session(name: str, cfg: dict, tmux: Tmux | None = None) -> dict:
    tmux = tmux or Tmux()
    root = Path(cfg["paths"]["root"])
    for hook in NEW_PANE_HOOKS:
        tmux.unset_hook(name, hook)
    panes = tmux.list_panes(name) if tmux.has_session(name) else []
    for p in panes:
        tmux.pipe_pane_off(p["pane_id"])
    removed = 0
    try:
        server_pid = tmux.server_pid()
        run_dir = root / "run" / str(server_pid)
        for p in panes:
            ptr = run_dir / p["pane_id"].lstrip("%")
            if ptr.exists():
                ptr.unlink()
                removed += 1
    except (subprocess.CalledProcessError, ValueError):
        pass
    return {"session": name, "pipes_closed": len(panes), "pointers_removed": removed}

def _current_session(tmux: Tmux) -> str | None:
    r = tmux.run("display-message", "-p", "#{session_name}", check=False)
    return r.stdout.strip() or None if r.returncode == 0 else None
