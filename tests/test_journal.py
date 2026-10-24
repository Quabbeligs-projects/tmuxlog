"""Unit tests for tmuxlog.journal (the hooks.rec parser).

Pure-function tests — no shell, no pty. Record byte-lengths are computed, never
hand-counted, so a fixture can't drift from the length prefix it declares.

Run with pytest, or standalone: ``python3 test_journal.py``.

Layout note: in the repo this imports ``from tmuxlog import journal``. The
try/except below also lets it run with journal.py sitting beside it. Keep
whichever import matches your package layout.
"""

from __future__ import annotations

import io
import json
import os
import tempfile

try:
    from tmuxlog import journal
except ImportError:  # journal.py beside this file
    import journal


# ---------------------------------------------------------------------------
# record builders (lengths computed from the real bytes)
# ---------------------------------------------------------------------------

def _b(x) -> bytes:
    return x if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8")


def _lf(value, last: bool) -> bytes:
    v = _b(value)
    return str(len(v)).encode() + b":" + v + (b"\n" if last else b" ")


def rec_I(nonce, ts, pid, shell, version=None, shlvl=None) -> bytes:
    out = b"I %s %s %s %s" % (_b(nonce), _b(ts), _b(pid), _b(shell))
    if version is not None:
        out += b" " + _b(version)
    if shlvl is not None:
        out += b" " + _b(shlvl)
    return out + b"\n"


def rec_S(seq, ts, nonce, shlvl, cmd, cwd, user, host, src=None) -> bytes:
    out = b"S %s %s %s %s " % (_b(seq), _b(ts), _b(nonce), _b(shlvl))
    if src is not None:
        out += _b(src) + b" "
    out += _lf(cmd, False) + _lf(cwd, False) + _lf(user, False) + _lf(host, True)
    return out


def rec_T(seq, ts, nonce, part, cmd) -> bytes:
    return b"T %s %s %s %s " % (_b(seq), _b(ts), _b(nonce), _b(part)) + _lf(cmd, True)


def rec_E(seq, ts, nonce, exit_code) -> bytes:
    return b"E %s %s %s %s\n" % (_b(seq), _b(ts), _b(nonce), _b(exit_code))


# ---------------------------------------------------------------------------
# parse() — record shapes
# ---------------------------------------------------------------------------

def test_I_four_fields():
    recs, warn = journal.parse(rec_I("n1", 100, 42, "bash"))
    assert warn == []
    assert recs == [{"type": "I", "nonce": "n1", "ts_ns": "100",
                     "pid": "42", "shell": "bash"}]


def test_I_six_fields():
    recs, warn = journal.parse(rec_I("n1", 100, 42, "bash", "5.2.15", 3))
    assert warn == []
    r = recs[0]
    assert r["shell_version"] == "5.2.15" and r["shlvl"] == "3"


def test_I_five_fields_version_only():
    recs, warn = journal.parse(rec_I("n1", 100, 42, "bash", "5.2.15"))
    assert warn == []
    r = recs[0]
    assert r["shell_version"] == "5.2.15" and "shlvl" not in r


def test_I_too_many_fields_is_corrupt():
    buf = b"I a b c d e f g\n"
    recs, warn = journal.parse(buf)
    assert recs == [] and warn and warn[0]["warning"] == "corrupt_record"


def test_S_with_src():
    recs, _ = journal.parse(rec_S(1, 100, "n1", 1, "ls -la", "/root", "root", "kal", src="bc"))
    r = recs[0]
    assert r["src"] == "bc" and r["cmd"] == "ls -la"


def test_S_without_src_backcompat():
    recs, _ = journal.parse(rec_S(1, 100, "n1", 1, "ls -la", "/root", "root", "kal"))
    assert recs[0]["src"] is None


def test_S_leading_space_command_preserved():
    # the ignorespace case: a space-prefixed command must survive byte-exact
    recs, _ = journal.parse(rec_S(1, 100, "n1", 1, " nmap -sV ", "/root", "root", "kal", src="bc"))
    assert recs[0]["cmd"] == " nmap -sV "


def test_S_embedded_newline_colon_heredoc():
    cmd = "cat <<EOF\nline: with: colons\nEOF"
    recs, warn = journal.parse(rec_S(1, 100, "n1", 1, cmd, "/root", "root", "kal", src="hist"))
    assert warn == []
    assert recs[0]["cmd"] == cmd


def test_S_non_utf8_byte_in_cmd():
    cmd = b"hydra -P rock\xff.txt"
    recs, warn = journal.parse(rec_S(1, 100, "n1", 1, cmd, "/root", "root", "kal", src="bc"))
    assert warn == []
    # bytes survive into the record via surrogateescape (lossless round-trip)
    assert recs[0]["cmd"].encode("utf-8", "surrogateescape") == cmd


def test_T_shares_seq():
    buf = (rec_S(7, 100, "n1", 1, "echo x", "/root", "root", "kal", src="bc")
           + rec_T(7, 101, "n1", 1, "cat"))
    recs, _ = journal.parse(buf)
    assert recs[1]["type"] == "T"
    assert recs[1]["seq"] == recs[0]["seq"] == "7"
    assert recs[1]["part"] == "1" and recs[1]["cmd"] == "cat"


def test_E_record():
    recs, _ = journal.parse(rec_E(1, 100, "n1", 130))
    assert recs[0] == {"type": "E", "seq": "1", "ts_ns": "100",
                       "nonce": "n1", "exit": "130"}


# ---------------------------------------------------------------------------
# parse() — resilience
# ---------------------------------------------------------------------------

def test_torn_trailing_record_one_warning():
    good = rec_I("n1", 100, 42, "bash", "5.2", 1) + rec_E(1, 200, "n1", 0)
    torn = b"S 2 300 n1 1 bc 99:too short"  # declares 99 bytes, not present
    recs, warn = journal.parse(good + torn)
    assert len(recs) == 2  # the two complete records survive
    assert len(warn) == 1 and warn[0]["warning"] == "torn_record"
    assert warn[0]["offset"] == len(good)


def test_corrupt_middle_record_skips_and_continues():
    good1 = rec_I("n1", 100, 42, "bash")
    bad = b"Z garbage line here\n"            # unknown type, has a newline after
    good2 = rec_E(1, 200, "n1", 0)
    recs, warn = journal.parse(good1 + bad + good2)
    assert [r["type"] for r in recs] == ["I", "E"]  # both good records kept
    assert warn and warn[0]["warning"] == "corrupt_record"


def test_empty_buffer():
    assert journal.parse(b"") == ([], [])


# ---------------------------------------------------------------------------
# to_events()
# ---------------------------------------------------------------------------

def test_events_kinds_and_pipeline():
    buf = (rec_I("n1", 100, 42, "bash", "5.2.15", 1)
           + rec_S(1, 110, "n1", 1, "echo hi", "/root", "root", "kal", src="bc")
           + rec_T(1, 111, "n1", 1, "base64")
           + rec_T(1, 112, "n1", 2, "cat")
           + rec_E(1, 200, "n1", 0))
    recs, _ = journal.parse(buf)
    evs = list(journal.to_events(recs))
    assert [e["event"] for e in evs] == [
        "init", "cmd_start", "cmd_part", "cmd_part", "cmd_end"]
    # the whole pipeline is captured, all stages share seq 1
    assert evs[2]["seq"] == evs[3]["seq"] == 1
    assert evs[2]["part"] == 1 and evs[3]["part"] == 2
    assert evs[2]["cmd"] == "base64" and evs[3]["cmd"] == "cat"


def test_events_types_are_ints():
    buf = rec_I("n1", 100, 42, "bash", "5.2", 2) + rec_E(5, 200, "n1", 130)
    evs = list(journal.to_events(journal.parse(buf)[0]))
    assert evs[0]["ts_ns"] == 100 and evs[0]["pid"] == 42 and evs[0]["shlvl"] == 2
    assert evs[1]["seq"] == 5 and evs[1]["exit"] == 130


def test_events_src_present_or_absent():
    with_src = journal.parse(rec_S(1, 1, "n1", 1, "a", "/", "u", "h", src="px"))[0]
    without = journal.parse(rec_S(1, 1, "n1", 1, "a", "/", "u", "h"))[0]
    assert list(journal.to_events(with_src))[0]["src"] == "px"
    assert "src" not in list(journal.to_events(without))[0]


def test_events_non_utf8_becomes_replacement_but_json_valid():
    recs = journal.parse(rec_S(1, 1, "n1", 1, b"x\xffy", "/", "u", "h", src="bc"))[0]
    ev = list(journal.to_events(recs))[0]
    line = json.dumps(ev, ensure_ascii=False)       # must not raise
    assert "�" in ev["cmd"] and json.loads(line)["cmd"] == ev["cmd"]


# ---------------------------------------------------------------------------
# file + jsonl plumbing
# ---------------------------------------------------------------------------

def test_events_from_file_roundtrip():
    buf = rec_I("n1", 100, 42, "bash") + rec_E(1, 200, "n1", 0)
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, buf)
        os.close(fd)
        events, warn = journal.events_from_file(path)
        assert warn == [] and [e["event"] for e in events] == ["init", "cmd_end"]
    finally:
        os.unlink(path)


def test_write_jsonl_is_valid_lines():
    buf = (rec_I("n1", 100, 42, "bash")
           + rec_S(1, 110, "n1", 1, "a b", "/", "u", "h", src="bc")
           + rec_E(1, 200, "n1", 0))
    out = io.StringIO()
    journal.write_jsonl(journal.to_events(journal.parse(buf)[0]), out)
    lines = out.getvalue().splitlines()
    assert len(lines) == 3
    for ln in lines:
        json.loads(ln)  # each line is standalone valid JSON


# ---------------------------------------------------------------------------
# standalone runner (so it works without pytest installed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback
    tests = sorted(k for k, v in list(globals().items())
                   if k.startswith("test_") and callable(v))
    passed = failed = 0
    for name in tests:
        try:
            globals()[name]()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
