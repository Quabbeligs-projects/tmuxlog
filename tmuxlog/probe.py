"""Empirical tmux capability probe: decides the in-band boundary mechanism.

Runs on a throwaway tmux server (`-L tmuxlog-probe-<pid> -f /dev/null`):

1. pipe-pane -O passthrough: a pane prints text interleaved with the private
   tmuxlog OSC (BEL- and ST-terminated) and an OSC 133 mark; the probe checks
   the exact bytes arrive in the pipe.
2. Invisibility: capture-pane must show the surrounding text with nothing
   between it, i.e. tmux swallowed the marks instead of rendering them.
3. Hook names: which pane-creation hooks this tmux accepts (3.x has no
   pane-created).
4. Pane close: kill-pane must give the pipe command EOF (the capture process
   relies on that to seal a pane, since pane-exited does not fire for it).

`boundary` is "osc" if 1 and 2 hold, else "sentinel".

    python3 -m tmuxlog.probe [--tmux /path/to/tmux]
"""

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

# printf format typed into the pane; octal escapes keep the typed echo free of
# the raw bytes, so only the program's output can match EXPECTED.
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
        # The marks are invisible iff the program's OUTPUT line shows the
        # surrounding text with EMPTY delimiters (PRB<>A<>B<>END) and carries no
        # mark bytes. We look only at that output line, NOT the whole screen: the
        # echoed command line legitimately contains the mark text as typed
        # (\033, 6973, tmuxlog, 133), so scanning the whole screen would always
        # report a false leak. Layout — a shell-prompt prefix, wrapping, a
        # trailing cursor — is ignored by matching VISIBLE as a substring with
        # spaces removed; a real leak would put bytes between the < > delimiters
        # (so "PRB<>" no longer appears) or leave an ESC on the line.
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="probe tmux for tmuxlog boundary support")
    p.add_argument("--tmux", default="tmux")
    args = p.parse_args(argv)
    print(json.dumps(probe(args.tmux), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
