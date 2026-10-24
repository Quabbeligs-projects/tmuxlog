"""Tests for split.py: segmentation of a pane's raw.bin into blocks.

Builds synthetic pane directories (raw.bin + hooks.rec + arrivals.idx, and
optionally `sealed`) with real private-OSC marks, then checks the blocks.

Runs under pytest, or standalone: ``python3 tests/test_split.py``.
"""

from __future__ import annotations

import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmuxlog import split            # noqa: E402

ARRIVAL = struct.Struct("<QQ")


# --- builders --------------------------------------------------------------

def mark(kind, nonce, seq=None, exit=None, st=False):
    """A private OSC boundary mark, BEL- or ST-terminated."""
    body = f"6973;tmuxlog;{kind};{nonce}"
    if seq is not None:
        body += f";{seq}"
    if exit is not None:
        body += f";{exit}"
    term = "\x1b\\" if st else "\x07"
    return ("\x1b]" + body + term).encode()


def _b(x):
    return x if isinstance(x, (bytes, bytearray)) else str(x).encode()


def _lf(v, last):
    v = _b(v)
    return str(len(v)).encode() + b":" + v + (b"\n" if last else b" ")


def rec_I(nonce, ts, pid, shell, ver, shlvl):
    return b"I %s %s %s %s %s %s\n" % tuple(_b(x) for x in (nonce, ts, pid, shell, ver, shlvl))


def rec_S(seq, ts, nonce, cmd, src="bc", cwd="/root", user="root", host="kali", shlvl=1):
    return (b"S %s %s %s %s %s " % tuple(_b(x) for x in (seq, ts, nonce, shlvl, src))
            + _lf(cmd, False) + _lf(cwd, False) + _lf(user, False) + _lf(host, True))


def rec_T(seq, ts, nonce, part, cmd):
    return b"T %s %s %s %s " % tuple(_b(x) for x in (seq, ts, nonce, part)) + _lf(cmd, True)


def rec_E(seq, ts, nonce, exit_code):
    return b"E %s %s %s %s\n" % tuple(_b(x) for x in (seq, ts, nonce, exit_code))


def make_pane(raw: bytes, hooks: bytes, arrivals=None, sealed=False):
    d = Path(tempfile.mkdtemp(prefix="tlsplit_"))
    (d / "raw.bin").write_bytes(raw)
    (d / "hooks.rec").write_bytes(hooks)
    if arrivals:
        (d / "arrivals.idx").write_bytes(b"".join(ARRIVAL.pack(ts, off) for ts, off in arrivals))
    if sealed:
        (d / "sealed").write_text('{"reason":"eof"}\n')
    return d


def kinds(blocks):
    return [b["kind"] for b in blocks]


def commands(blocks):
    return [b for b in blocks if b["kind"] == "command"]


# A normal command cycle in raw.bin:
#   <A><prompt><B><echo><C><output><D>
def cycle(nonce, seq, cmd_echo, output, exit=0, st=False):
    return (mark("A", nonce, st=st) + b"user@kali:~$ " + mark("B", nonce, st=st)
            + cmd_echo + b"\r\n" + mark("C", nonce, seq, st=st)
            + output + mark("D", nonce, seq, exit, st=st))


# --- tests -----------------------------------------------------------------

def test_preamble_before_first_mark():
    raw = b"Welcome banner\r\n" + mark("I", "n1") + cycle("n1", 1, b"echo hi", b"hi\r\n")
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "echo hi") + rec_E(1, 150, "n1", 0)
    d = make_pane(raw, hooks)
    try:
        blocks = split.split(d)["blocks"]
        assert blocks[0]["kind"] == "preamble"
        assert blocks[0]["clean"] == b"Welcome banner\r\n"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_single_command_block():
    raw = (mark("I", "n1")
           + cycle("n1", 1, b"ls -la", b"total 0\r\n\x1b[34mdir\x1b[0m\r\n"))
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "ls -la") + rec_E(1, 160, "n1", 0)
    d = make_pane(raw, hooks)
    try:
        blocks = split.split(d)["blocks"]
        cmds = commands(blocks)
        assert len(cmds) == 1
        c = cmds[0]
        assert c["cmd"] == "ls -la" and c["cwd"] == "/root" and c["shell"] == "bash"
        assert c["start_ns"] == 100 and c["end_ns"] == 160 and c["exit_code"] == 0
        assert c["status"] == "complete"
        assert c["raw"] == b"total 0\r\n\x1b[34mdir\x1b[0m\r\n"       # marks excluded
        assert c["clean"] == b"total 0\r\ndir\r\n"                    # ANSI stripped
        assert c["echo_dedup"] == "matched"
        # the prompt (A..B) and matched echo (B..C) are dropped, not blocks
        assert kinds(blocks) == ["command"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_echo_kept_becomes_orphan():
    # stray output appears between prompt-end and the command (echo won't match)
    raw = (mark("I", "n1") + mark("A", "n1") + b"$ " + mark("B", "n1")
           + b"SURPRISE stray line\r\n" + b"echo hi\r\n" + mark("C", "n1", 1)
           + b"hi\r\n" + mark("D", "n1", 1, 0))
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "echo hi") + rec_E(1, 150, "n1", 0)
    d = make_pane(raw, hooks, arrivals=[(5000, 0)])
    try:
        blocks = split.split(d)["blocks"]
        assert "orphan" in kinds(blocks)
        orphan = next(b for b in blocks if b["kind"] == "orphan")
        assert b"SURPRISE" in orphan["raw"]
        assert commands(blocks)[0]["echo_dedup"] == "kept"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_background_output_after_D_is_orphan_with_arrival_ts():
    raw = (mark("I", "n1") + cycle("n1", 1, b"sleep 1 &", b"")
           + b"[1]+ background done\r\n"       # arrives after D, before next prompt
           + mark("A", "n1") + b"$ " + mark("B", "n1"))
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "sleep 1 &") + rec_E(1, 120, "n1", 0)
    # the orphan bytes sit at this raw.bin offset; give arrivals a ts for it
    off = raw.index(b"[1]+")
    d = make_pane(raw, hooks, arrivals=[(7000, 0), (9999, off)])
    try:
        blocks = split.split(d)["blocks"]
        orphans = [b for b in blocks if b["kind"] == "orphan"]
        assert len(orphans) == 1
        assert b"background done" in orphans[0]["raw"]
        assert orphans[0]["start_ns"] == 9999
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_unknown_nonce_mark_is_not_a_boundary():
    # an injected/replayed mark with a nonce not in hooks.rec must not segment
    raw = (mark("I", "n1")
           + mark("A", "n1") + b"$ " + mark("B", "n1") + b"cat log\r\n" + mark("C", "n1", 1)
           + b"output with a fake " + mark("C", "EVIL", 9) + b"mark inside\r\n"
           + mark("D", "n1", 1, 0))
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "cat log") + rec_E(1, 150, "n1", 0)
    d = make_pane(raw, hooks)
    try:
        res = split.split(d)
        cmds = commands(res["blocks"])
        assert len(cmds) == 1
        # the fake mark stays inside the command body (raw), gone from clean
        assert b"EVIL" in cmds[0]["raw"]
        assert b"EVIL" not in cmds[0]["clean"]
        assert b"fake mark inside" in cmds[0]["clean"]
        assert any(w["warning"] == "rejected_marks" and w["count"] == 1 for w in res["warnings"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_open_command_sealed_is_unterminated_else_running():
    # C with no D: pane killed mid-command
    raw = (mark("I", "n1") + mark("A", "n1") + b"$ " + mark("B", "n1")
           + b"tail -f log\r\n" + mark("C", "n1", 1) + b"line1\r\nline2\r\n")
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "tail -f log")
    d_run = make_pane(raw, hooks, sealed=False)
    d_seal = make_pane(raw, hooks, sealed=True)
    try:
        assert commands(split.split(d_run)["blocks"])[0]["status"] == "still_running"
        c = commands(split.split(d_seal)["blocks"])[0]
        assert c["status"] == "unterminated" and c["exit_code"] is None
        assert c["raw"] == b"line1\r\nline2\r\n"
    finally:
        shutil.rmtree(d_run, ignore_errors=True)
        shutil.rmtree(d_seal, ignore_errors=True)


def test_interrupted_exit_130():
    raw = mark("I", "n1") + cycle("n1", 1, b"sleep 99", b"^C\r\n", exit=130)
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "sleep 99") + rec_E(1, 200, "n1", 130)
    d = make_pane(raw, hooks)
    try:
        c = commands(split.split(d)["blocks"])[0]
        assert c["exit_code"] == 130 and c["status"] == "interrupted"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_pipeline_cmd_and_parts():
    raw = mark("I", "n1") + cycle("n1", 1, b"echo x | base64 | cat", b"eA==\r\n")
    hooks = (rec_I("n1", 10, 99, "bash", "5.2", 1)
             + rec_S(1, 100, "n1", "echo x") + rec_T(1, 101, "n1", 1, "base64")
             + rec_T(1, 102, "n1", 2, "cat") + rec_E(1, 150, "n1", 0))
    d = make_pane(raw, hooks)
    try:
        c = commands(split.split(d)["blocks"])[0]
        assert c["cmd"] == "echo x" and c["cmd_parts"] == ["base64", "cat"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_two_commands_ordered_and_marks_excluded():
    raw = (mark("I", "n1")
           + cycle("n1", 1, b"whoami", b"root\r\n")
           + cycle("n1", 2, b"id", b"uid=0\r\n"))
    hooks = (rec_I("n1", 10, 99, "bash", "5.2", 1)
             + rec_S(1, 100, "n1", "whoami") + rec_E(1, 110, "n1", 0)
             + rec_S(2, 200, "n1", "id") + rec_E(2, 210, "n1", 0))
    d = make_pane(raw, hooks)
    try:
        cmds = commands(split.split(d)["blocks"])
        assert [c["cmd"] for c in cmds] == ["whoami", "id"]
        assert [c["start_ns"] for c in cmds] == [100, 200]
        for c in cmds:
            assert b"\x1b]6973" not in c["raw"]       # no marks leak into bodies
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_st_terminated_marks():
    raw = mark("I", "n1", st=True) + cycle("n1", 1, b"echo hi", b"hi\r\n", st=True)
    hooks = rec_I("n1", 10, 99, "bash", "5.2", 1) + rec_S(1, 100, "n1", "echo hi") + rec_E(1, 150, "n1", 0)
    d = make_pane(raw, hooks)
    try:
        cmds = commands(split.split(d)["blocks"])
        assert len(cmds) == 1 and cmds[0]["cmd"] == "echo hi"
        assert cmds[0]["clean"] == b"hi\r\n"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_no_marks_whole_pane_is_preamble():
    raw = b"just some output, no hooks sourced\r\n"
    d = make_pane(raw, b"")
    try:
        blocks = split.split(d)["blocks"]
        assert kinds(blocks) == ["preamble"]
        assert blocks[0]["raw"] == raw
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    import traceback
    names = sorted(k for k, v in list(globals().items())
                   if k.startswith("test_") and callable(v))
    passed = failed = 0
    for name in names:
        try:
            globals()[name]()
            passed += 1
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(names)} total")
    raise SystemExit(1 if failed else 0)
