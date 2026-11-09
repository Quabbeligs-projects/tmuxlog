import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

HOOKS = ["pane-created", "after-new-session", "after-new-window",
         "after-split-window", "after-respawn-pane", "pane-exited", "session-closed"]

PRINTF = r"printf 'PRB<\033]6973;tmuxlog;C;probe;1\007>A<\033]6973;tmuxlog;D;probe;1;0\033\\>B<\033]133;C\007>END\n'"
EXPECTED = (b"PRB<\x1b]6973;tmuxlog;C;probe;1\x07>A<\x1b]6973;tmuxlog;D;probe;1;0\x1b\\>B<"
            b"\x1b]133;C\x07>END")
VISIBLE = "PRB<>A<>B<>END"

def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()

def probe(tmux: str = "tmux") -> dict:
    sock = f"tmuxlog-probe-{os.getpid()}"
    base = [tmux, "-L", sock, "-f", "/dev/null"]

    def t(*args, check=True):
        return subprocess.run(base + list(args), capture_output=True, text=True, check=check)

    tmp = Path(tempfile.mkdtemp(prefix="tmuxlog-probe-"))
    raw, eof = tmp / "raw", tmp / "eof"
    result = {"tmux": tmux, "version": subprocess.run([tmux, "-V"], capture_output=True,
                                                      text=True).stdout.strip()}
    try:
        t("new-session", "-d", "-s", "probe", "-x", "120", "-y", "20",
          "env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}", "PS1=$ ", "sh")
        t("pipe-pane", "-O", "-t", "probe:0.0", f"cat >> '{raw}'; echo EOF > '{eof}'")
        t("send-keys", "-t", "probe:0.0", PRINTF, "Enter")
        _wait(lambda: raw.exists() and b">END" in raw.read_bytes().split(b"\n", 1)[-1])
        data = raw.read_bytes() if raw.exists() else b""
        result["osc_passthrough_exact"] = EXPECTED in data
        screen = t("capture-pane", "-p", "-t", "probe:0.0").stdout
        result["screen_line"] = next((line for line in screen.splitlines() if "END" in line
                                      and "printf" not in line), None)
        line = result["screen_line"] or ""
        result["osc_invisible"] = (
            VISIBLE in line.replace(" ", "")
            and "\x1b" not in line
            and "6973" not in line
        )

        result["hooks"] = {}
        for hook in HOOKS:
            r = t("set-hook", "-g", hook, "display-message ok", check=False)
            result["hooks"][hook] = r.returncode == 0
            if r.returncode == 0:
                t("set-hook", "-gu", hook, check=False)

        t("kill-pane", "-t", "probe:0.0", check=False)
        result["kill_pane_gives_pipe_eof"] = _wait(eof.exists, 3.0)
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    ok = result.get("osc_passthrough_exact") and result.get("osc_invisible")
    result["boundary"] = "osc" if ok else "sentinel"
    return result

def _server(tmux: str, tag: str) -> list[str]:
    return [tmux, "-L", f"tmuxlog-probe-{tag}-{os.getpid()}", "-f", "/dev/null"]

def _sh() -> list[str]:
    return ["env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}", "PS1=$ ", "sh"]

def probe_session_hooks(tmux: str = "tmux") -> dict:
    """Do session-scoped after-* hooks fire only for their session, with the new pane's id?"""
    base = _server(tmux, "hooks")
    tmp = Path(tempfile.mkdtemp(prefix="tmuxlog-probe-"))
    log = tmp / "hooks.log"

    def t(*args, check=True):
        return subprocess.run(base + list(args), capture_output=True, text=True, check=check)

    def hook_cmd(tag):
        return f"run-shell -b \"echo {tag} #{{session_name}} #{{pane_id}} >> '{log}'\""

    def lines():
        return log.read_text().split() if log.exists() else []

    res = {}
    try:
        t("new-session", "-d", "-s", "a", *_sh())
        t("new-session", "-d", "-s", "b", *_sh())
        t("set-hook", "-t", "a", "after-split-window", hook_cmd("split"))
        t("set-hook", "-t", "a", "after-new-window", hook_cmd("neww"))
        t("set-hook", "-g", "after-new-session", hook_cmd("news"))

        new = t("split-window", "-P", "-F", "#{pane_id}", "-t", "a", *_sh()).stdout.strip()
        _wait(lambda: "split" in lines(), 3.0)
        res["split_in_scope_fires_with_new_pane_id"] = lines()[:3] == ["split", "a", new]

        t("split-window", "-t", "b", *_sh())
        new = t("new-window", "-P", "-F", "#{pane_id}", "-t", "a", *_sh()).stdout.strip()
        _wait(lambda: "neww" in lines(), 3.0)
        got = lines()
        res["split_other_session_does_not_fire"] = got.count("split") == 1
        res["new_window_in_scope_fires_with_new_pane_id"] = got[3:6] == ["neww", "a", new]

        new = t("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "c", *_sh()).stdout.strip()
        _wait(lambda: "news" in lines(), 3.0)
        got = lines()
        res["global_new_session_fires"] = got[-3:] == ["news", "c", new]
        res["session_hook_lines"] = log.read_text().splitlines() if log.exists() else []
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return res

SENTINEL = b"@@TMUXLOG;C;probe;1@@"
SENTINEL_PRINTF = r"printf 'S<@@TMUX%sLOG;C;probe;1@@\033[21D\033[K>E\n' ''"

def probe_nested(tmux: str = "tmux") -> dict:
    """What an OUTER pipe-pane sees when the marks are printed inside a nested tmux."""
    outer, inner = _server(tmux, "outer"), _server(tmux, "inner")
    tmp = Path(tempfile.mkdtemp(prefix="tmuxlog-probe-"))
    raw = tmp / "raw"

    def run(base, *args, check=True):
        return subprocess.run(base + list(args), capture_output=True, text=True, check=check)

    res = {}
    try:
        run(outer, "start-server", ";", "set", "-g", "default-terminal", "screen", ";",
            "new-session", "-d", "-s", "o", "-x", "120", "-y", "20",
            "env", "-u", "TMUX", *inner, "new-session", "-s", "i", *_sh())
        res["inner_started"] = _wait(
            lambda: run(inner, "has-session", "-t", "i", check=False).returncode == 0, 5.0)
        if not res["inner_started"]:
            return res
        time.sleep(0.5)
        run(outer, "pipe-pane", "-O", "-t", "o:0.0", f"cat >> '{raw}'")
        run(inner, "send-keys", "-t", "i", PRINTF, "Enter")
        run(inner, "send-keys", "-t", "i", SENTINEL_PRINTF, "Enter")
        time.sleep(1.5)
        data = raw.read_bytes() if raw.exists() else b""
        res["outer_bytes"] = len(data)
        res["osc_reaches_outer_pipe"] = EXPECTED in data
        res["private_osc_bytes_in_outer_pipe"] = b"\x1b]6973;" in data
        res["sentinel_reaches_outer_pipe"] = SENTINEL in data
        screen = run(outer, "capture-pane", "-p", "-t", "o:0.0").stdout
        res["outer_screen_osc_line"] = next((l for l in screen.splitlines() if "END" in l and "printf" not in l), None)
        res["outer_screen_sentinel_line"] = next((l for l in screen.splitlines() if l.strip().endswith(">E")
                                                  and "printf" not in l), None)
    finally:
        subprocess.run(inner + ["kill-server"], capture_output=True)
        subprocess.run(outer + ["kill-server"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return res

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="probe tmux for tmuxlog boundary support")
    p.add_argument("--tmux", default="tmux")
    p.add_argument("--extended", action="store_true",
                   help="also probe session-scoped hooks and nested tmux")
    args = p.parse_args(argv)
    result = probe(args.tmux)
    if args.extended:
        result["session_hooks"] = probe_session_hooks(args.tmux)
        result["nested"] = probe_nested(args.tmux)
    print(json.dumps(result, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
