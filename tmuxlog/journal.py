from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

class _Torn(Exception):
class _Corrupt(Exception):

# low-level byte readers
def _token(buf: bytes, i: int) -> tuple[bytes, int]:
    """Read a space-delimited token at offset ``i``; return (token, next_offset)."""
    sp = buf.find(b" ", i)
    if sp == -1:
        raise _Torn("eof reading token")
    return buf[i:sp], sp + 1

def _peek_is_lenfield(buf: bytes, i: int) -> bool:
    sp = buf.find(b" ", i)
    co = buf.find(b":", i)
    if co == -1:
        return False
    if sp != -1 and sp < co:
        return False
    return buf[i:co].isdigit()

def _lenfield(buf: bytes, i: int, last: bool) -> tuple[bytes, int]:
    """Read one ``<len>:<value>`` field. ``last`` fields end in \\n, others a space."""
    colon = buf.find(b":", i)
    if colon == -1:
        raise _Torn("eof reading length prefix")
    raw_len = buf[i:colon]
    if not raw_len.isdigit():
        raise _Corrupt(f"bad length prefix {raw_len!r}")
    length = int(raw_len)
    vstart = colon + 1
    vend = vstart + length
    if vend > len(buf):
        raise _Torn("eof reading value")
    value = buf[vstart:vend]
    sep = buf[vend:vend + 1]
    expected = b"\n" if last else b" "
    if sep != expected:
        if sep == b"":
            raise _Torn("eof reading field separator")
        raise _Corrupt(f"expected {expected!r} after length field, got {sep!r}")
    return value, vend + 1

def _text(b: bytes) -> str:
    """Decode a byte field, preserving exotic bytes via surrogateescape."""
    return b.decode("utf-8", "surrogateescape")

# record parsing
def _parse_I(buf: bytes, i: int) -> tuple[dict, int]:
    nl = buf.find(b"\n", i)
    if nl == -1:
        raise _Torn("eof reading I record")
    parts = buf[i:nl].split(b" ")
    got = len(parts) - 1 
    if got < 4 or got > 6:
        raise _Corrupt(f"I record has {got} fields, want 4-6")
    rec = {
        "type": "I",
        "nonce": _text(parts[1]),
        "ts_ns": _text(parts[2]),
        "pid": _text(parts[3]),
        "shell": _text(parts[4]),
    }
    if got >= 5:
        rec["shell_version"] = _text(parts[5])
    if got >= 6:
        rec["shlvl"] = _text(parts[6])
    return rec, nl + 1

def _parse_E(buf: bytes, i: int) -> tuple[dict, int]:
    nl = buf.find(b"\n", i)
    if nl == -1:
        raise _Torn("eof reading E record")
    parts = buf[i:nl].split(b" ")
    if len(parts) - 1 != 4:
        raise _Corrupt(f"E record has {len(parts) - 1} fields, want 4")
    return {
        "type": "E",
        "seq": _text(parts[1]),
        "ts_ns": _text(parts[2]),
        "nonce": _text(parts[3]),
        "exit": _text(parts[4]),
    }, nl + 1

def _parse_S(buf: bytes, i: int) -> tuple[dict, int]:
    i += 2 
    seq, i = _token(buf, i)
    ts_ns, i = _token(buf, i)
    nonce, i = _token(buf, i)
    shlvl, i = _token(buf, i)
    src = None
    if not _peek_is_lenfield(buf, i):
        src_b, i = _token(buf, i)
        src = _text(src_b)
    cmd, i = _lenfield(buf, i, last=False)
    cwd, i = _lenfield(buf, i, last=False)
    user, i = _lenfield(buf, i, last=False)
    host, i = _lenfield(buf, i, last=True)
    rec = {
        "type": "S",
        "seq": _text(seq),
        "ts_ns": _text(ts_ns),
        "nonce": _text(nonce),
        "shlvl": _text(shlvl),
        "src": src,
        "cmd": _text(cmd),
        "cwd": _text(cwd),
        "user": _text(user),
        "host": _text(host),
    }
    return rec, i

def _parse_T(buf: bytes, i: int) -> tuple[dict, int]:
    i += 2 
    seq, i = _token(buf, i)
    ts_ns, i = _token(buf, i)
    nonce, i = _token(buf, i)
    part, i = _token(buf, i)
    cmd, i = _lenfield(buf, i, last=True)
    return {
        "type": "T",
        "seq": _text(seq),
        "ts_ns": _text(ts_ns),
        "nonce": _text(nonce),
        "part": _text(part),
        "cmd": _text(cmd),
    }, i

def _parse_one(buf: bytes, i: int) -> tuple[dict, int]:
    t = buf[i:i + 1]
    if t == b"S":
        if buf[i:i + 2] != b"S ":
            raise _Torn("eof after type char")
        return _parse_S(buf, i)
    if t == b"T":
        if buf[i:i + 2] != b"T ":
            raise _Torn("eof after type char")
        return _parse_T(buf, i)
    if t == b"E":
        return _parse_E(buf, i)
    if t == b"I":
        return _parse_I(buf, i)
    raise _Corrupt(f"unknown record type {t!r}")

def parse(buf: bytes) -> tuple[list[dict], list[dict]]:
    records: list[dict] = []
    warnings: list[dict] = []
    i, n = 0, len(buf)
    while i < n:
        start = i
        try:
            rec, i = _parse_one(buf, i)
        except _Torn as e:
            warnings.append({"warning": "torn_record", "offset": start,
                             "bytes": n - start, "detail": str(e)})
            break
        except _Corrupt as e:
            nl = buf.find(b"\n", i)
            if nl == -1:
                warnings.append({"warning": "corrupt_record", "offset": start,
                                 "bytes": n - start, "detail": str(e)})
                break
            warnings.append({"warning": "corrupt_record", "offset": start,
                             "bytes": nl + 1 - start, "detail": str(e)})
            i = nl + 1
            continue
        records.append(rec)
    return records, warnings

# event emission
def _int(s: str) -> int:
    return int(s)

def _safe(s: str) -> str:
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")

def to_events(records: list[dict]) -> Iterator[dict]:
    for r in records:
        t = r["type"]
        if t == "I":
            ev = {
                "event": "init",
                "nonce": r["nonce"],
                "ts_ns": _int(r["ts_ns"]),
                "pid": _int(r["pid"]),
                "shell": r["shell"],
            }
            if "shell_version" in r:
                ev["shell_version"] = r["shell_version"]
            if "shlvl" in r:
                ev["shlvl"] = _int(r["shlvl"])
            yield ev
        elif t == "S":
            ev = {
                "event": "cmd_start",
                "seq": _int(r["seq"]),
                "ts_ns": _int(r["ts_ns"]),
                "nonce": r["nonce"],
                "depth": _int(r["shlvl"]),
                "cmd": _safe(r["cmd"]),
                "cwd": _safe(r["cwd"]),
                "user": _safe(r["user"]),
                "host": _safe(r["host"]),
            }
            if r.get("src") is not None:
                ev["src"] = r["src"]
            yield ev
        elif t == "T":
            yield {
                "event": "cmd_part",
                "seq": _int(r["seq"]),
                "ts_ns": _int(r["ts_ns"]),
                "nonce": r["nonce"],
                "part": _int(r["part"]),
                "cmd": _safe(r["cmd"]),
            }
        elif t == "E":
            yield {
                "event": "cmd_end",
                "seq": _int(r["seq"]),
                "ts_ns": _int(r["ts_ns"]),
                "nonce": r["nonce"],
                "exit": _int(r["exit"]),
            }

def events_from_file(path: str | Path) -> tuple[list[dict], list[dict]]:
    buf = Path(path).read_bytes()
    records, warnings = parse(buf)
    return list(to_events(records)), warnings

def write_jsonl(events: Iterator[dict], out) -> None:
    for ev in events:
        out.write(json.dumps(ev, ensure_ascii=False, sort_keys=False))
        out.write("\n")

# CLI + self-test
def _selftest() -> int:
    new = (
        b"I abc123 1700000000000000000 4242 bash 5.2.15 1\n"
        b"S 1 1700000000111111000 abc123 1 bc "
        b"32:cat <<EOF\nline: with: colons\nEOF 5:/root 4:root 3:kal\n"
        b"T 1 1700000000111222000 abc123 1 3:cat\n"
        b"E 1 1700000000222222000 abc123 0\n"
        b"S 2 1700000000333333000 abc123 1 10: nmap -sV  5:/root 4:root 3:kal\n"
        b"E 2 1700000000444444000 abc123 0\n"
    )
    torn = b"S 3 1700000000555555000 abc123 1 bc 99:too short"
    records, warnings = parse(new + torn)
    assert len(records) == 6, [r["type"] for r in records]
    assert warnings and warnings[0]["warning"] == "torn_record", warnings
    evs = list(to_events(records))
    kinds = [e["event"] for e in evs]
    assert kinds == ["init", "cmd_start", "cmd_part", "cmd_end",
                     "cmd_start", "cmd_end"], kinds
    assert evs[0]["shell_version"] == "5.2.15" and evs[0]["shlvl"] == 1
    assert evs[1]["src"] == "bc"
    assert evs[1]["cmd"] == "cat <<EOF\nline: with: colons\nEOF"
    assert evs[2]["event"] == "cmd_part" and evs[2]["part"] == 1 and evs[2]["cmd"] == "cat"
    assert evs[4]["cmd"] == " nmap -sV " and "src" not in evs[4]
    assert evs[5]["exit"] == 0
    print("selftest OK:", len(records), "records,", len(warnings), "warning(s)")
    return 0

def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m tmuxlog.journal <hooks.rec>   # emit JSONL to stdout\n"
              "       python -m tmuxlog.journal --selftest", file=sys.stderr)
        return 0 if argv else 2
    if argv[0] == "--selftest":
        return _selftest()
    events, warnings = events_from_file(argv[0])
    write_jsonl(iter(events), sys.stdout)
    for w in warnings:
        print("warning:", json.dumps(w), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
