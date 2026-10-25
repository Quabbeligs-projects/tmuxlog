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

        # No warm-up Enter: the shells drew their first prompt before
        # start_session created the arm pointers, so the first real command
        # below must arm its shell at preexec (I record before the first S).

        # THE RACE: p0 starts a long command first; p1 a short one just after.
        # p1 finishes first (output arrives first); p0 started first.
        tmux("send-keys", "-t", p0, "sleep 0.8; echo LONGDONE", "Enter")
        time.sleep(0.1)
        tmux("send-keys", "-t", p1, "echo SHORTFAST", "Enter")

        # wait for both commands to complete (each pane has exactly the one real
        # command's cmd_end)
        assert _wait(lambda: _done_count(pdir[p0]) >= 1 and _done_count(pdir[p1]) >= 1, timeout=20)
        for p in (p0, p1):   # preexec arm: init precedes the first cmd_start
            kinds = [e["event"] for e in _events(pdir[p])]
            assert kinds[:2] == ["init", "cmd_start"], kinds

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


def test_e2e_full_pipeline_three_panes_through_finalize():
    """start_session -> hook-armed new window -> race + orphan -> stop -> finalize -> final/."""
    if not shutil.which("tmux"):
        _skip("tmux not installed")
    import hashlib
    import json
    from tmuxlog import ansi, finalize

    tmp = Path(tempfile.mkdtemp(prefix="tle2e_"))
    home = tmp / "home"; home.mkdir()
    root = tmp / "root"; root.mkdir()
    sock = f"tle2ef-{os.getpid()}"
    bashrc = home / ".bashrc"
    bashrc.write_text(f"PS1='RDY$ '\nHISTFILE={home}/.hist\nsource {INIT}\n")
    env = {**os.environ, "HOME": str(home), "TMUXLOG_ROOT": str(root),
           "PYTHONPATH": str(REPO), "TERM": "xterm"}
    shell = f"bash --rcfile {bashrc} --noprofile -i"
    base = ["tmux", "-L", sock, "-f", "/dev/null"]
    cfg = {"paths": {"root": str(root)}}
    tm = Tmux(base)

    def tmux(*a):
        return subprocess.run(base + list(a), env=env, capture_output=True, text=True)

    def pane_dir_of(pane_id):
        ptr = root / "run" / str(tm.server_pid()) / pane_id.lstrip("%")
        return Path(ptr.read_text().strip()) if ptr.exists() else None

    try:
        subprocess.run(base + ["new-session", "-d", "-s", "full", "-x", "200", "-y", "50", shell],
                       env=env, check=True)
        tmux("split-window", "-t", "full", shell)
        assert _wait(lambda: len(tm.list_panes("full")) == 2)
        res = start_session("full", cfg, tmux=tm, boundary="osc")
        assert [p["status"] for p in res["panes"]] == ["armed", "armed"], res["panes"]
        session_dir = Path(res["dir"])

        # a pane created AFTER start_session is armed by the session-scoped hook
        new = tmux("new-window", "-P", "-F", "#{pane_id}", "-t", "full", shell).stdout.strip()
        assert _wait(lambda: pane_dir_of(new) is not None and
                     tm.run("display-message", "-p", "-t", new, "#{pane_pipe}").stdout.strip() == "1")
        p0, p1 = (p["pane_id"] for p in res["panes"])
        pdir = {p0: Path(res["panes"][0]["pane_dir"]), p1: Path(res["panes"][1]["pane_dir"]),
                new: pane_dir_of(new)}

        tmux("send-keys", "-t", p0, "printf 'scan: \\033[1;31m22/tcp open\\033[0m\\n'", "Enter")
        assert _wait(lambda: _done_count(pdir[p0]) >= 1)
        # the race, across windows: long starts first in p0, short just after in the new window
        tmux("send-keys", "-t", p0, "sleep 1; echo LONG-SCAN-DONE", "Enter")
        time.sleep(0.15)
        tmux("send-keys", "-t", new, "echo SHORT-ENUM-DONE", "Enter")
        # background job in p1: its late output must become an orphan block. A
        # plain `cmd &` (bash does not attribute async lists/groups, see DESIGN §8)
        tmux("send-keys", "-t", p1, "sh -c 'sleep 0.4; echo ORPHAN-LATE' &", "Enter")
        assert _wait(lambda: _done_count(pdir[p0]) >= 2 and _done_count(pdir[new]) >= 1
                     and _done_count(pdir[p1]) >= 1, timeout=20)
        assert _wait(lambda: b"ORPHAN-LATE" in (pdir[p1] / "raw.bin").read_bytes())

        stop_session("full", cfg, tmux=tm)
        assert _wait(lambda: all((d / "sealed").exists() for d in pdir.values()), timeout=10)
        finalize.finalize(session_dir)

        fin = session_dir / "final"
        man = json.loads((fin / "manifest.json").read_text())
        master = (fin / "master.log").read_bytes()
        clean = (fin / "master.clean.log").read_bytes()
        blocks = man["blocks"]
        cmds = [b for b in blocks if b["kind"] == "command"]

        # every pane, including the hook-armed one, contributed command blocks
        assert {b["pane_uid"] for b in cmds} == {d.name for d in pdir.values()}
        # final order is by start_ns, never by arrival
        starts = [b["start_ns"] for b in blocks if b["start_ns"] is not None]
        assert starts == sorted(starts)
        longc = next(b for b in cmds if b["cmd"] == "sleep 1; echo LONG-SCAN-DONE")
        shortc = next(b for b in cmds if b["cmd"] == "echo SHORT-ENUM-DONE")
        assert longc["start_ns"] < shortc["start_ns"] and longc["end_ns"] > shortc["end_ns"]
        assert cmds.index(longc) < cmds.index(shortc)
        assert all(b["status"] == "complete" and b["exit_code"] == 0 for b in cmds)

        # atomicity: each block body holds only its own pane's output
        bodies = {}
        for chunk in clean.split(b"===== BLOCK ")[1:]:
            bid = int(chunk.split(b" ", 1)[0])
            bodies[bid] = chunk.split(b"----- output -----\n", 1)[1].rsplit(b"===== END BLOCK", 1)[0]
        assert b"LONG-SCAN-DONE" in bodies[longc["block_id"]]
        assert b"SHORT-ENUM-DONE" not in bodies[longc["block_id"]]
        assert b"LONG-SCAN-DONE" not in bodies[shortc["block_id"]]
        orphans = [b for b in blocks if b["kind"] == "orphan" and b"ORPHAN-LATE" in bodies[b["block_id"]]]
        assert len(orphans) == 1 and orphans[0]["pane_uid"] == pdir[p1].name

        # evidence integrity
        assert ansi.strip(master) == clean and b"\x1b[1;31m" in master
        sha = lambda d: hashlib.sha256(d).hexdigest()
        for name, info in man["files"].items():
            assert sha((fin / name).read_bytes()) == info["sha256"], name
        for b in blocks:
            a, e = b["raw_offsets"]
            body = (session_dir / "panes" / b["pane_uid"] / "raw.bin").read_bytes()[a:e]
            assert sha(body) == b["sha256_raw"], b["block_id"]
        assert man["gaps"] == []
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
