import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from tmuxlog import paths

NEW_PANE_HOOKS = ("after-split-window", "after-new-window")
PANE_FIELDS = ["pane_id", "pane_pid", "session_name", "window_index", "window_name",
               "pane_index", "pane_title", "pane_tty", "pane_pipe"]
_PANE_FMT = "\t".join("#{%s}" % f for f in PANE_FIELDS)

PKG_ROOT = Path(__file__).resolve().parent.parent


class Tmux:
    def __init__(self, base=("tmux",)):
        self.base = list(base)

    def run(self, *args, check=True):
        return subprocess.run(self.base + list(args), capture_output=True, text=True, check=check)

    def has_session(self, name):
        return self.run("has-session", "-t", name, check=False).returncode == 0

    def server_pid(self):
        return int(self.run("display-message", "-p", "#{pid}").stdout.strip())

    def new_session_detached(self, name):
        self.run("new-session", "-d", "-s", name)

    def list_panes(self, name):
        out = self.run("list-panes", "-s", "-t", name, "-F", _PANE_FMT).stdout
        panes = []
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) < 9:
                continue
            panes.append({"pane_id": f[0], "pane_pid": f[1], "session": f[2],
                          "window_index": f[3], "window_name": f[4], "pane_index": f[5],
                          "pane_title": f[6], "tty": f[7], "piped": f[8] == "1"})
        return panes

    def pipe_pane(self, pane_id, cmd):
        self.run("pipe-pane", "-O", "-t", pane_id, cmd)

    def pipe_pane_off(self, pane_id):
        r = self.run("display-message", "-p", "-t", pane_id, "#{pane_pipe}", check=False)
        if r.stdout.strip() == "1":
            self.run("pipe-pane", "-t", pane_id, check=False)

    def set_hook(self, name, hook, cmd):
        self.run("set-hook", "-t", name, hook, cmd)

    def unset_hook(self, name, hook):
        self.run("set-hook", "-u", "-t", name, hook, check=False)


def _launcher(cfg, exec_=False):
    cfg = cfg or {}
    root = Path(cfg.get("paths", {}).get("root", "~/.tmuxlog")).expanduser()
    parts = ["TMUXLOG_ROOT=" + shlex.quote(str(root))]
    custom = (cfg.get("capture", {}).get("cmd") or "").strip()
    if custom:
        if custom.endswith(" capture"):
            custom = custom[:-8]
        cmd = custom
    else:
        parts.append("PYTHONPATH=" + shlex.quote(str(PKG_ROOT)))
        cmd = shlex.quote(sys.executable) + " -m tmuxlog"
    if exec_:
        parts.append("exec")
    parts.append(cmd)
    return " ".join(parts)


def _capture_cmd(cfg):
    return _launcher(cfg, exec_=True) + " capture"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _owns_pipe(pane_dir):
    f = pane_dir / "capture.pid"
    if not f.exists():
        return False
    try:
        pid = int(f.read_text().split()[0])
    except (ValueError, IndexError, OSError):
        return False
    return _pid_alive(pid)


def arm_pane(pane, session_dir, root, server_pid, boundary, tmux, cfg=None):
    num = pane["pane_id"].lstrip("%")
    run_dir = paths.ensure_dir(root / "run" / str(server_pid))
    ptr = run_dir / num

    if ptr.exists():
        pane_dir = Path(ptr.read_text().strip())
    else:
        pane_dir = session_dir / "panes" / f"{num}-{server_pid}-{time.time_ns()}"
    paths.ensure_dir(pane_dir)

    if not (pane_dir / "meta.json").exists():
        meta = {
            "meta_version": 1,
            "pane_uid": pane_dir.name,
            "pane_id": pane["pane_id"],
            "pane_num": int(num),
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
        paths.write_private_atomic(pane_dir / "meta.json", (json.dumps(meta) + "\n").encode())
    paths.open_private(pane_dir / "hooks.rec", "ab").close()
    paths.write_private_atomic(pane_dir / "boundary", (boundary + "\n").encode())
    paths.write_private_atomic(ptr, (str(pane_dir) + "\n").encode())

    if not pane["piped"]:
        tmux.pipe_pane(pane["pane_id"], _capture_cmd(cfg) + " --pane-dir " + shlex.quote(str(pane_dir)))
        status = "armed"
    elif _owns_pipe(pane_dir):
        status = "already-ours"
    else:
        status = "foreign-pipe"
    return {"pane_id": pane["pane_id"], "pane_dir": str(pane_dir), "status": status}


def _current_session(tmux):
    r = tmux.run("display-message", "-p", "#{session_name}", check=False)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def start_session(name, cfg, tmux=None, boundary="osc"):
    tmux = tmux or Tmux()
    root = Path(cfg["paths"]["root"])

    if not name and os.environ.get("TMUX"):
        name = _current_session(tmux)
    if not name:
        raise SystemExit("tmuxlog: no session name and not inside tmux")
    created = False
    if not tmux.has_session(name):
        tmux.new_session_detached(name)
        created = True

    server_pid = tmux.server_pid()
    sdir = paths.ensure_dir(root / "sessions" / name)
    results = []
    for p in tmux.list_panes(name):
        results.append(arm_pane(p, sdir, root, server_pid, boundary, tmux, cfg))

    me = _launcher(cfg) + " arm-new --session " + shlex.quote(name)
    hooks = []
    for hook in cfg.get("tmux", {}).get("hooks") or NEW_PANE_HOOKS:
        try:
            tmux.set_hook(name, hook, "run-shell " + shlex.quote(me))
        except subprocess.CalledProcessError as e:
            print(f"tmuxlog: couldn't set hook {hook}: {(e.stderr or '').strip()}", file=sys.stderr)
            continue
        hooks.append(hook)

    info = {"name": name, "server_pid": server_pid, "created_ns": time.time_ns(),
            "boundary": boundary, "hooks": hooks, "created_session": created}
    paths.write_private_atomic(sdir / "session.json", (json.dumps(info) + "\n").encode())
    return {"session": name, "dir": str(sdir), "created": created, "boundary": boundary, "panes": results}


def arm_new(name, cfg, tmux=None):
    tmux = tmux or Tmux()
    root = Path(cfg["paths"]["root"])
    sdir = root / "sessions" / name
    boundary = "osc"
    if (sdir / "session.json").exists():
        boundary = json.loads((sdir / "session.json").read_text()).get("boundary", "osc")
    if not tmux.has_session(name):
        return {"session": name, "panes": []}
    server_pid = tmux.server_pid()
    run_dir = root / "run" / str(server_pid)
    out = []
    for p in tmux.list_panes(name):
        if p["piped"] and (run_dir / p["pane_id"].lstrip("%")).exists():
            continue
        out.append(arm_pane(p, sdir, root, server_pid, boundary, tmux, cfg))
    return {"session": name, "panes": out}


def stop_session(name, cfg, tmux=None):
    tmux = tmux or Tmux()
    root = Path(cfg["paths"]["root"])
    sdir = root / "sessions" / name

    hooks = set(NEW_PANE_HOOKS) | set(cfg.get("tmux", {}).get("hooks") or [])
    if (sdir / "session.json").exists():
        try:
            hooks |= set(json.loads((sdir / "session.json").read_text()).get("hooks", []))
        except (OSError, ValueError):
            pass
    for hook in sorted(hooks):
        tmux.unset_hook(name, hook)

    panes = []
    if tmux.has_session(name):
        panes = tmux.list_panes(name)
    for p in panes:
        tmux.pipe_pane_off(p["pane_id"])

    removed = 0
    mine = sdir / "panes"
    run = root / "run"
    if run.is_dir():
        for d in run.iterdir():
            if not d.is_dir():
                continue
            for ptr in d.iterdir():
                try:
                    target = Path(ptr.read_text().strip())
                except OSError:
                    continue
                if target.parent == mine:
                    ptr.unlink()
                    removed += 1
            if d.name.isdigit() and not _pid_alive(int(d.name)) and not any(d.iterdir()):
                d.rmdir()
    return {"session": name, "pipes_closed": len(panes), "pointers_removed": removed}
