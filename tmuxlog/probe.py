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

SENTINEL = b"@@TMUXLOG;C;probe;1@@"
SENTINEL_PRINTF = r"printf 'S<@@TMUX%sLOG;C;probe;1@@\033[21D\033[K>E\n' ''"


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def server(tmux, tag):
    return [tmux, "-L", "tmuxlog-probe-%s-%d" % (tag, os.getpid()), "-f", "/dev/null"]


def plain_sh():
    return ["env", "-i", "PATH=" + os.environ.get("PATH", "/usr/bin:/bin"), "PS1=$ ", "sh"]


def tm(base, *args, check=True):
    return subprocess.run(base + list(args), capture_output=True, text=True, check=check)


def probe(tmux="tmux"):
    base = [tmux, "-L", f"tmuxlog-probe-{os.getpid()}", "-f", "/dev/null"]
    tmp = Path(tempfile.mkdtemp(prefix="tmuxlog-probe-"))
    raw = tmp / "raw"
    eof = tmp / "eof"
    ver = subprocess.run([tmux, "-V"], capture_output=True, text=True).stdout.strip()
    result = {"tmux": tmux, "version": ver}
    try:
        tm(base, "new-session", "-d", "-s", "probe", "-x", "120", "-y", "20", *plain_sh())
        tm(base, "pipe-pane", "-O", "-t", "probe:0.0", f"cat >> '{raw}'; echo EOF > '{eof}'")
        tm(base, "send-keys", "-t", "probe:0.0", PRINTF, "Enter")
        wait_for(lambda: raw.exists() and b">END" in raw.read_bytes().split(b"\n", 1)[-1])
        data = raw.read_bytes() if raw.exists() else b""
        result["osc_passthrough_exact"] = EXPECTED in data

        screen = tm(base, "capture-pane", "-p", "-t", "probe:0.0").stdout
        line = None
        for l in screen.splitlines():
            if "END" in l and "printf" not in l:
                line = l
                break
        result["screen_line"] = line
        line = line or ""
        result["osc_invisible"] = VISIBLE in line.replace(" ", "") and "\x1b" not in line and "6973" not in line

        result["hooks"] = {}
        for h in HOOKS:
            r = tm(base, "set-hook", "-g", h, "display-message ok", check=False)
            result["hooks"][h] = r.returncode == 0
            if r.returncode == 0:
                tm(base, "set-hook", "-gu", h, check=False)

        tm(base, "kill-pane", "-t", "probe:0.0", check=False)
        result["kill_pane_gives_pipe_eof"] = wait_for(eof.exists, 3.0)
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    if result.get("osc_passthrough_exact") and result.get("osc_invisible"):
        result["boundary"] = "osc"
    else:
        result["boundary"] = "sentinel"
    return result


def probe_session_hooks(tmux="tmux"):
    base = server(tmux, "hooks")
    tmp = Path(tempfile.mkdtemp(prefix="tmuxlog-probe-"))
    log = tmp / "hooks.log"

    def hook(tag):
        return f"run-shell -b \"echo {tag} #{{session_name}} #{{pane_id}} >> '{log}'\""

    def words():
        if log.exists():
            return log.read_text().split()
        return []

    res = {}
    try:
        tm(base, "new-session", "-d", "-s", "a", *plain_sh())
        tm(base, "new-session", "-d", "-s", "b", *plain_sh())
        tm(base, "set-hook", "-t", "a", "after-split-window", hook("split"))
        tm(base, "set-hook", "-t", "a", "after-new-window", hook("neww"))
        tm(base, "set-hook", "-g", "after-new-session", hook("news"))

        pane = tm(base, "split-window", "-P", "-F", "#{pane_id}", "-t", "a", *plain_sh()).stdout.strip()
        wait_for(lambda: "split" in words(), 3.0)
        res["split_fires"] = words()[:3] == ["split", "a", pane]

        tm(base, "split-window", "-t", "b", *plain_sh())
        pane = tm(base, "new-window", "-P", "-F", "#{pane_id}", "-t", "a", *plain_sh()).stdout.strip()
        wait_for(lambda: "neww" in words(), 3.0)
        w = words()
        res["split_other_session_quiet"] = w.count("split") == 1
        res["neww_fires"] = w[3:6] == ["neww", "a", pane]

        pane = tm(base, "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "c", *plain_sh()).stdout.strip()
        wait_for(lambda: "news" in words(), 3.0)
        res["global_news_fires"] = words()[-3:] == ["news", "c", pane]
        res["log"] = log.read_text().splitlines() if log.exists() else []
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def probe_nested(tmux="tmux"):
    outer = server(tmux, "outer")
    inner = server(tmux, "inner")
    tmp = Path(tempfile.mkdtemp(prefix="tmuxlog-probe-"))
    raw = tmp / "raw"
    res = {}
    try:
        tm(outer, "start-server", ";", "set", "-g", "default-terminal", "screen", ";",
           "new-session", "-d", "-s", "o", "-x", "120", "-y", "20",
           "env", "-u", "TMUX", *inner, "new-session", "-s", "i", *plain_sh())
        res["inner_started"] = wait_for(lambda: tm(inner, "has-session", "-t", "i", check=False).returncode == 0, 5.0)
        if not res["inner_started"]:
            return res
        time.sleep(0.5)
        tm(outer, "pipe-pane", "-O", "-t", "o:0.0", f"cat >> '{raw}'")
        tm(inner, "send-keys", "-t", "i", PRINTF, "Enter")
        tm(inner, "send-keys", "-t", "i", SENTINEL_PRINTF, "Enter")
        time.sleep(1.5)
        data = b""
        if raw.exists():
            data = raw.read_bytes()
        res["outer_bytes"] = len(data)
        res["osc_in_outer"] = EXPECTED in data
        res["private_osc_in_outer"] = b"\x1b]6973;" in data
        res["sentinel_in_outer"] = SENTINEL in data
        lines = tm(outer, "capture-pane", "-p", "-t", "o:0.0").stdout.splitlines()
        res["outer_osc_line"] = next((l for l in lines if "END" in l and "printf" not in l), None)
        res["outer_sentinel_line"] = next((l for l in lines if l.strip().endswith(">E") and "printf" not in l), None)
    finally:
        subprocess.run(inner + ["kill-server"], capture_output=True)
        subprocess.run(outer + ["kill-server"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def main(argv=None):
    p = argparse.ArgumentParser(description="probe tmux for tmuxlog boundary support")
    p.add_argument("--tmux", default="tmux")
    p.add_argument("--extended", action="store_true", help="also probe session-scoped hooks and nested tmux")
    args = p.parse_args(argv)
    result = probe(args.tmux)
    if args.extended:
        result["session_hooks"] = probe_session_hooks(args.tmux)
        result["nested"] = probe_nested(args.tmux)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
