"""Tests for the tmuxlog CLI (events / status / export / init-script).

Builds a session tree of per-pane hooks.rec files with known records, then
drives the real cli.py functions. Session resolution uses --session omitted
(most-recent-by-mtime) so the tests don't depend on paths.session_dir internals;
cfg is a minimal dict with the one key cli reads.

Runs under pytest, or standalone: ``python3 tests/test_cli.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo -> tmuxlog

from tmuxlog import cli          # noqa: E402


# --- record builders (byte lengths computed, never hand-counted) -----------

def _b(x):
    return x if isinstance(x, (bytes, bytearray)) else str(x).encode()


def _lf(v, last):
    v = _b(v)
    return str(len(v)).encode() + b":" + v + (b"\n" if last else b" ")


def _I(nonce, ts, pid, shell, ver, shlvl):
    return b"I %s %s %s %s %s %s\n" % (_b(nonce), _b(ts), _b(pid), _b(shell), _b(ver), _b(shlvl))


def _S(seq, ts, nonce, cmd, src="bc", cwd="/root", user="root", host="kal", shlvl=1):
    return (b"S %s %s %s %s %s " % (_b(seq), _b(ts), _b(nonce), _b(shlvl), _b(src))
            + _lf(cmd, False) + _lf(cwd, False) + _lf(user, False) + _lf(host, True))


def _E(seq, ts, nonce, exit_code):
    return b"E %s %s %s %s\n" % (_b(seq), _b(ts), _b(nonce), _b(exit_code))


def _session(tmp: Path):
    """root/sessions/sess/panes/{p0,p1}/hooks.rec, with p0 holding one open cmd."""
    root = tmp / "root"
    sess = root / "sessions" / "sess"
    p0 = sess / "panes" / "p0"
    p1 = sess / "panes" / "p1"
    p0.mkdir(parents=True)
    p1.mkdir(parents=True)
    # p0: init@50, "echo a"@100 (closed), "sleep 99"@300 (OPEN, no E)
    (p0 / "hooks.rec").write_bytes(
        _I("n0", 50, 10, "bash", "5.2", 1)
        + _S(1, 100, "n0", "echo a") + _E(1, 150, "n0", 0)
        + _S(2, 300, "n0", "sleep 99")
    )
    # p1: init@60, "echo b"@200 (closed, exit 2)
    (p1 / "hooks.rec").write_bytes(
        _I("n1", 60, 11, "zsh", "5.9", 1)
        + _S(1, 200, "n1", "echo b", src="px") + _E(1, 250, "n1", 2)
    )
    return root, sess, p0, p1


def _cfg(root):
    return {"paths": {"root": str(root)}}


def _capture(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **k)
    return rc, buf.getvalue()


# --- tests -----------------------------------------------------------------

def test_pane_dirs_sorted_and_filtered():
    tmp = Path(tempfile.mkdtemp())
    try:
        _, sess, p0, p1 = _session(tmp)
        (sess / "panes" / "empty").mkdir()        # no hooks.rec -> excluded
        dirs = cli._pane_dirs(sess)
        assert [d.name for d in dirs] == ["p0", "p1"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_open_commands_detection():
    tmp = Path(tempfile.mkdtemp())
    try:
        _, _, p0, _ = _session(tmp)
        events, _ = cli.journal.events_from_file(p0 / "hooks.rec")
        open_cmds = cli._open_commands(events)
        assert [e["cmd"] for e in open_cmds] == ["sleep 99"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_events_to_stdout():
    tmp = Path(tempfile.mkdtemp())
    try:
        _, _, p0, _ = _session(tmp)
        args = SimpleNamespace(target=str(p0), output="-")
        rc, out = _capture(cli.cmd_events, args, None)   # cfg unused by cmd_events
        assert rc == 0
        rows = [json.loads(l) for l in out.splitlines()]
        kinds = [r["event"] for r in rows]
        assert kinds == ["init", "cmd_start", "cmd_end", "cmd_start"]
        assert rows[1]["cmd"] == "echo a"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_events_to_file_is_private():
    tmp = Path(tempfile.mkdtemp())
    try:
        _, _, p0, _ = _session(tmp)
        out = p0 / "events.jsonl"
        args = SimpleNamespace(target=str(p0), output=str(out))
        rc, _ = _capture(cli.cmd_events, args, None)
        assert rc == 0 and out.is_file()
        assert (out.stat().st_mode & 0o777) == 0o600
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        assert rows[0]["event"] == "init"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_session_events_merged_in_timestamp_order():
    tmp = Path(tempfile.mkdtemp())
    try:
        root, sess, _, _ = _session(tmp)
        events, warnings = cli.session_events(sess)
        assert warnings == []
        ts = [e["ts_ns"] for e in events]
        assert ts == sorted(ts)                        # globally ordered
        # the interleave: p1's echo b (200) sits between p0's 150 and 300
        seq = [(e["pane"], e["event"], e["ts_ns"]) for e in events]
        assert ("p1", "cmd_start", 200) in seq
        assert all("pane" in e for e in events)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_jsonl_orders_across_panes():
    tmp = Path(tempfile.mkdtemp())
    try:
        root, _, _, _ = _session(tmp)
        args = SimpleNamespace(format="jsonl", session=None)
        rc, out = _capture(cli.cmd_export, args, _cfg(root))
        assert rc == 0
        ts = [json.loads(l)["ts_ns"] for l in out.splitlines()]
        assert ts == sorted(ts) and ts[0] == 50
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_reports_counts_and_open():
    tmp = Path(tempfile.mkdtemp())
    try:
        root, _, _, _ = _session(tmp)
        args = SimpleNamespace(session=None)
        rc, out = _capture(cli.cmd_status, args, _cfg(root))
        assert rc == 0
        assert "p0:" in out and "p1:" in out
        assert "1 open" in out                         # p0 has the open sleep
        assert "sleep 99" in out                       # open command shown
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_rejects_unimplemented_format():
    tmp = Path(tempfile.mkdtemp())
    try:
        root, _, _, _ = _session(tmp)
        args = SimpleNamespace(format="raw", session=None)
        try:
            cli.cmd_export(args, _cfg(root))
            assert False, "expected SystemExit"
        except SystemExit as e:
            assert "not implemented" in str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_parser_wires_subcommands():
    p = cli.build_parser()
    ns = p.parse_args(["events", "somedir", "-o", "-"])
    assert ns.func is cli.cmd_events and ns.target == "somedir" and ns.output == "-"
    ns = p.parse_args(["export", "--format", "jsonl"])
    assert ns.func is cli.cmd_export and ns.format == "jsonl"


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
