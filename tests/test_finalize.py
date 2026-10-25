"""Tests for finalize.py: merging pane blocks into the master log + manifest.

Builds synthetic session trees (panes with raw.bin + hooks.rec + arrivals.idx +
meta.json, optional gaps.jsonl) and checks ordering, sha256 integrity,
truncation, the gaps banner, and the raw-vs-clean invariant.

Standalone: ``python3 tests/test_finalize.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmuxlog import ansi, finalize            # noqa: E402

ARRIVAL = struct.Struct("<QQ")


# --- builders (reuse the split test shapes) --------------------------------

def mark(kind, nonce, seq=None, exit=None):
    body = f"6973;tmuxlog;{kind};{nonce}"
    if seq is not None:
        body += f";{seq}"
    if exit is not None:
        body += f";{exit}"
    return ("\x1b]" + body + "\x07").encode()


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


def rec_E(seq, ts, nonce, exit_code):
    return b"E %s %s %s %s\n" % tuple(_b(x) for x in (seq, ts, nonce, exit_code))


def cycle(nonce, seq, cmd_echo, output, exit=0):
    return (mark("A", nonce) + b"$ " + mark("B", nonce)
            + cmd_echo + b"\r\n" + mark("C", nonce, seq)
            + output + mark("D", nonce, seq, exit))


def make_pane(session_dir, uid, pane_id, pane_num, raw, hooks, arrivals=None,
              meta_extra=None, sealed=True, gaps=None):
    d = session_dir / "panes" / uid
    d.mkdir(parents=True)
    (d / "raw.bin").write_bytes(raw)
    (d / "hooks.rec").write_bytes(hooks)
    if arrivals:
        (d / "arrivals.idx").write_bytes(b"".join(ARRIVAL.pack(t, o) for t, o in arrivals))
    if sealed:
        (d / "sealed").write_text('{"reason":"eof"}\n')
    if gaps:
        (d / "gaps.jsonl").write_text("".join(json.dumps(g) + "\n" for g in gaps))
    meta = {"pane_uid": uid, "pane_id": pane_id, "pane_num": pane_num,
            "session": "sess", "window_index": "0", "pane_index": pane_num,
            "window_name": "w", "pane_title": "t", "created_ns": 1}
    meta.update(meta_extra or {})
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def session_with_two_panes(tmp):
    sd = tmp / "sessions" / "sess"
    sd.mkdir(parents=True)
    # pane 0: a long command that STARTS first (start_ns 100)
    make_pane(sd, "0-1-1", "%0", "0",
              mark("I", "a") + cycle("a", 1, b"nmap -sV", b"scan done\r\n"),
              rec_I("a", 10, 1, "bash", "5.2", 1) + rec_S(1, 100, "a", "nmap -sV") + rec_E(1, 900, "a", 0))
    # pane 1: a short command that STARTS later (200) but FINISHES earlier (250)
    make_pane(sd, "1-1-2", "%1", "1",
              mark("I", "b") + cycle("b", 1, b"whoami", b"root\r\n"),
              rec_I("b", 20, 2, "bash", "5.2", 1) + rec_S(1, 200, "b", "whoami") + rec_E(1, 250, "b", 0))
    return sd


# --- tests -----------------------------------------------------------------

def test_cross_pane_order_by_start_ns():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = session_with_two_panes(tmp)
        finalize.finalize(sd)
        mani = json.loads((sd / "final" / "manifest.json").read_text())
        cmds = [b for b in mani["blocks"] if b["kind"] == "command"]
        # nmap started first (100) though it finished last (900); whoami at 200
        assert [c["cmd"] for c in cmds] == ["nmap -sV", "whoami"]
        assert [c["start_ns"] for c in cmds] == [100, 200]
        # block_ids are sequential in final order
        assert [c["block_id"] for c in cmds] == sorted(c["block_id"] for c in cmds)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_all_master_files_written():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = session_with_two_panes(tmp)
        finalize.finalize(sd)
        final = sd / "final"
        for name in ("master.log", "master.clean.log", "events.jsonl", "manifest.json"):
            assert (final / name).exists(), name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sha256_per_block_matches_body():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = session_with_two_panes(tmp)
        finalize.finalize(sd)
        mani = json.loads((sd / "final" / "manifest.json").read_text())
        nmap = next(b for b in mani["blocks"] if b.get("cmd") == "nmap -sV")
        # the recorded sha256 is of the real block body (output between C and D)
        assert nmap["sha256_raw"] == hashlib.sha256(b"scan done\r\n").hexdigest()
        assert nmap["sha256_clean"] == hashlib.sha256(b"scan done\r\n").hexdigest()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_file_level_sha256_in_manifest():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = session_with_two_panes(tmp)
        finalize.finalize(sd)
        final = sd / "final"
        mani = json.loads((final / "manifest.json").read_text())
        for name in ("master.log", "master.clean.log", "events.jsonl"):
            got = hashlib.sha256((final / name).read_bytes()).hexdigest()
            assert mani["files"][name]["sha256"] == got, name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clean_differs_from_raw_only_in_escapes():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = tmp / "sessions" / "s"
        sd.mkdir(parents=True)
        out = b"total\r\n\x1b[31mred\x1b[0m\r\n"
        make_pane(sd, "0-1-1", "%0", "0",
                  mark("I", "a") + cycle("a", 1, b"ls", out),
                  rec_I("a", 10, 1, "bash", "5.2", 1) + rec_S(1, 100, "a", "ls") + rec_E(1, 150, "a", 0))
        finalize.finalize(sd)
        raw = (sd / "final" / "master.log").read_bytes()
        clean = (sd / "final" / "master.clean.log").read_bytes()
        # stripping escapes from the raw master yields exactly the clean master
        assert ansi.strip(raw) == clean
        assert b"\x1b[31m" in raw and b"\x1b[31m" not in clean
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_truncation_marker_and_hash_of_full_block():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = tmp / "sessions" / "s"
        sd.mkdir(parents=True)
        big = b"A" * 5000
        make_pane(sd, "0-1-1", "%0", "0",
                  mark("I", "a") + cycle("a", 1, b"dump", big),
                  rec_I("a", 10, 1, "bash", "5.2", 1) + rec_S(1, 100, "a", "dump") + rec_E(1, 150, "a", 0))
        finalize.finalize(sd, cap_bytes=1000)      # force truncation
        master = (sd / "final" / "master.log").read_bytes()
        mani = json.loads((sd / "final" / "manifest.json").read_text())
        blk = next(b for b in mani["blocks"] if b.get("cmd") == "dump")
        assert blk["truncated"] is True
        assert blk["bytes_total"] == 5000
        assert b"TRUNCATED" in master
        # the full-block hash is of all 5000 bytes, not the truncated copy
        assert blk["sha256_raw"] == hashlib.sha256(big).hexdigest()
        # master actually contains fewer than the full body
        assert master.count(b"A") < 5000
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gaps_surfaced_loudly():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = tmp / "sessions" / "s"
        sd.mkdir(parents=True)
        make_pane(sd, "0-1-1", "%0", "0",
                  mark("I", "a") + cycle("a", 1, b"x", b"y\r\n"),
                  rec_I("a", 10, 1, "bash", "5.2", 1) + rec_S(1, 100, "a", "x") + rec_E(1, 150, "a", 0),
                  gaps=[{"kind": "raw", "stream_start": 4, "stream_end": 9, "bytes": 5,
                         "first_ts_ns": 1, "last_ts_ns": 2, "error": "ENOSPC"}])
        finalize.finalize(sd)
        master = (sd / "final" / "master.log").read_bytes()
        mani = json.loads((sd / "final" / "manifest.json").read_text())
        assert b"WARNING" in master and b"INCOMPLETE" in master
        assert mani["gap_bytes"] == 5 and len(mani["gaps"]) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_atomic_no_tmp_left_and_rerun_stable():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = session_with_two_panes(tmp)
        finalize.finalize(sd)
        first = (sd / "final" / "master.log").read_bytes()
        finalize.finalize(sd)                       # rerun must be idempotent
        assert (sd / "final" / "master.log").read_bytes() == first
        assert not list((sd / "final").glob("*.tmp"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_events_jsonl_has_events_then_block_records():
    tmp = Path(tempfile.mkdtemp())
    try:
        sd = session_with_two_panes(tmp)
        finalize.finalize(sd)
        rows = [json.loads(l) for l in (sd / "final" / "events.jsonl").read_text().splitlines()]
        kinds = [r["event"] for r in rows]
        assert "cmd_start" in kinds and "block" in kinds
        # all hook events come before the block records
        assert max(i for i, k in enumerate(kinds) if k != "block") < \
               min(i for i, k in enumerate(kinds) if k == "block")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
