"""End-to-end test on a real headless tmux server.

Arms a two-pane session with start_session, drives the nmap/gobuster race (a
long command started first in one pane, a short command started just after in
the other, finishing first), tears down, and asserts the master ordering is by
command START timestamp — not output arrival — with each block containing only
its own pane's output. Also checks sealing.

Needs tmux on PATH; skips otherwise. Uses the OSC boundary (real tmux).

Standalone: ``python3 tests/test_e2e.py``  (run with PYTHONPATH=<repo>).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmuxlog import journal, split                    # noqa: E402
from tmuxlog.session import Tmux, start_session, stop_session   # noqa: E402

REPO = Path(__file__).resolve().parent.parent
INIT = REPO / "tmuxlog" / "shell" / "tmuxlog-init.sh"


class SkipTest(Exception):
    pass


def _skip(msg):
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        raise SkipTest(msg)


def _wait(pred, timeout=15.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _events(pane_dir):
    f = Path(pane_dir) / "hooks.rec"
    if not f.exists():
        return []
    return list(journal.to_events(journal.parse(f.read_bytes())[0]))


def _done_count(pane_dir):
    return sum(e["event"] == "cmd_end" for e in _events(pane_dir))


def _command_blocks(session_dir):
    out = []
    for pane in (Path(session_dir) / "panes").iterdir():
        for b in split.split(pane)["blocks"]:
            if b["kind"] == "command":
                b = {**b, "pane_uid": pane.name}
                out.append(b)
    return out


def test_e2e_race_ordering_and_atomicity():
    if not shutil.which("tmux"):
        _skip("tmux not installed")

    tmp = Path(tempfile.mkdtemp(prefix="tle2e_"))
    home = tmp / "home"; home.mkdir()
    root = tmp / "root"; root.mkdir()
    sock = f"tle2e-{os.getpid()}"
    bashrc = home / ".bashrc"
    bashrc.write_text(f"PS1='RDY$ '\nHISTFILE={home}/.hist\nsource {INIT}\n")

    env = {**os.environ, "HOME": str(home), "TMUXLOG_ROOT": str(root),
           "PYTHONPATH": str(REPO), "TERM": "xterm"}
    shell = f"bash --rcfile {bashrc} --noprofile -i"
    base = ["tmux", "-L", sock, "-f", "/dev/null"]
    cfg = {"paths": {"root": str(root)}}
    tm = Tmux(base)

    def tmux(*a, **k):
        return subprocess.run(base + list(a), env=env, capture_output=True, text=True, **k)

    try:
        # start the server (carries env -> pane shells + capture inherit it)
        subprocess.run(base + ["new-session", "-d", "-s", "sess", "-x", "200", "-y", "50", shell],
                       env=env, check=True)
        tmux("split-window", "-t", "sess", shell)
        _wait(lambda: len(tm.list_panes("sess")) == 2)
        panes = [p["pane_id"] for p in tm.list_panes("sess")]
        p0, p1 = panes[0], panes[1]

        # arm both panes (real start-session: pointers, pipe-pane, capture)
        res = start_session("sess", cfg, tmux=tm, boundary="osc")
        session_dir = Path(res["dir"])
        assert all(p["status"] == "armed" for p in res["panes"]), res["panes"]
        # capture processes write raw.bin
        assert _wait(lambda: all((Path(p["pane_dir"]) / "raw.bin").exists() for p in res["panes"]))
        pdir = {p["pane_id"]: Path(p["pane_dir"]) for p in res["panes"]}

        # warm-up Enter so each shell arms at its next prompt (pointer now exists)
        for p in (p0, p1):
            tmux("send-keys", "-t", p, "", "Enter")
        assert _wait(lambda: all((pdir[p] / "hooks.rec").stat().st_size > 0 for p in (p0, p1)))

        # THE RACE: p0 starts a long command first; p1 a short one just after.
        # p1 finishes first (output arrives first); p0 started first.
        tmux("send-keys", "-t", p0, "sleep 0.8; echo LONGDONE", "Enter")
        time.sleep(0.1)
        tmux("send-keys", "-t", p1, "echo SHORTFAST", "Enter")

        # wait for both commands to complete (warm-up Enter was empty -> no cmd;
        # each pane has exactly the one real command's cmd_end)
        assert _wait(lambda: _done_count(pdir[p0]) >= 1 and _done_count(pdir[p1]) >= 1, timeout=20)

        stop_session("sess", cfg, tmux=tm)
        assert _wait(lambda: all((pdir[p] / "sealed").exists() for p in (p0, p1)), timeout=10)

        # ---- assertions ----
        cmds = _command_blocks(session_dir)
        longc = next(c for c in cmds if b"LONGDONE" in c["clean"])
        shortc = next(c for c in cmds if b"SHORTFAST" in c["clean"])

        # started first finishes last: start order != finish order
        assert longc["start_ns"] < shortc["start_ns"], (longc["start_ns"], shortc["start_ns"])
        assert longc["end_ns"] > shortc["end_ns"], "long should finish after short"

        # master ordering is by start_ns -> long before short despite later arrival
        ordered = sorted(cmds, key=lambda c: (c["start_ns"], c["pane_uid"]))
        assert ordered.index(longc) < ordered.index(shortc)

        # block atomicity: each block holds only its own pane's output
        assert b"SHORTFAST" not in longc["clean"]
        assert b"LONGDONE" not in shortc["clean"]
        assert longc["status"] == "complete" and longc["exit_code"] == 0
        assert longc["pane_uid"] != shortc["pane_uid"]
        print(f"  race OK: long start={longc['start_ns']} end={longc['end_ns']}; "
              f"short start={shortc['start_ns']} end={shortc['end_ns']}")
    finally:
        subprocess.run(base + ["kill-server"], env=env, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def test_e2e_killpane_seals_capture():
    if not shutil.which("tmux"):
        _skip("tmux not installed")
    tmp = Path(tempfile.mkdtemp(prefix="tle2e_"))
    home = tmp / "home"; home.mkdir()
    root = tmp / "root"; root.mkdir()
    sock = f"tle2ek-{os.getpid()}"
    bashrc = home / ".bashrc"
    bashrc.write_text(f"PS1='RDY$ '\nHISTFILE={home}/.hist\nsource {INIT}\n")
    env = {**os.environ, "HOME": str(home), "TMUXLOG_ROOT": str(root),
           "PYTHONPATH": str(REPO), "TERM": "xterm"}
    shell = f"bash --rcfile {bashrc} --noprofile -i"
    base = ["tmux", "-L", sock, "-f", "/dev/null"]
    cfg = {"paths": {"root": str(root)}}
    tm = Tmux(base)
    try:
        subprocess.run(base + ["new-session", "-d", "-s", "k", "-x", "120", "-y", "40", shell],
                       env=env, check=True)
        _wait(lambda: len(tm.list_panes("k")) == 1)
        res = start_session("k", cfg, tmux=tm, boundary="osc")
        pane_dir = Path(res["panes"][0]["pane_dir"])
        assert _wait(lambda: (pane_dir / "raw.bin").exists())
        # killing the pane closes the pipe -> capture gets EOF -> seals
        subprocess.run(base + ["kill-pane", "-t", res["panes"][0]["pane_id"]], env=env,
                       capture_output=True)
        assert _wait(lambda: (pane_dir / "sealed").exists(), timeout=10)
    finally:
        subprocess.run(base + ["kill-server"], env=env, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import traceback
    names = sorted(k for k, v in list(globals().items())
                   if k.startswith("test_") and callable(v))
    passed = failed = skipped = 0
    for name in names:
        try:
            globals()[name]()
            passed += 1
            print(f"PASS {name}")
        except SkipTest as e:
            skipped += 1
            print(f"SKIP {name}: {e}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped, {len(names)} total")
    raise SystemExit(1 if failed else 0)
