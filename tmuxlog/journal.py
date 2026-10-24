"""Parse a per-pane ``hooks.rec`` journal into structured records and JSONL events.

``hooks.rec`` is append-only, written only by the shell hooks (docs/DESIGN.md §4).
Three record types, each terminated by a newline:

    I <nonce> <ts_ns> <pid> <shell>\\n
    S <seq> <ts_ns> <nonce> <shlvl> <len>:<cmd> <len>:<cwd> <len>:<user> <len>:<host>\\n
    E <seq> <ts_ns> <nonce> <exit>\\n

The four fields of an ``S`` record are length-prefixed (byte counts written with
``LC_ALL=C ${#v}``), so the command, cwd, user and host may contain spaces,
colons and newlines with no escaping. That is why everything here is parsed at
the byte level instead of line by line.

A ``hooks.rec`` can end in a torn record if the shell or box died mid-write.
The parser stops at the first incomplete trailing record and reports it as a
warning rather than raising — finalize (§6) relies on that for crash recovery.
A structurally broken record in the *middle* of the file is skipped up to the
next newline and parsing continues.

Scope: this module handles the shell-side I/S/E records only. The A/B/C/D OSC
boundary marks live in ``raw.bin`` and are the splitter's job in finalize, not
this parser's.

stdlib only (Python 3.11+).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator


class _Torn(Exception):
    """Ran off the end of the buffer mid-record — expected on crash recovery."""


class _Corrupt(Exception):
    """Structurally invalid record, but there is more data after it."""


# ---------------------------------------------------------------------------
# low-level byte readers
# ---------------------------------------------------------------------------

def _token(buf: bytes, i: int) -> tuple[bytes, int]:
    """Read a space-delimited token at offset ``i``; return (token, next_offset)."""
    sp = buf.find(b" ", i)
    if sp == -1:
        raise _Torn("eof reading token")
    return buf[i:sp], sp + 1


def _lenfield(buf: bytes, i: int, last: bool) -> tuple[bytes, int]:
    """Read one ``<len>:<value>`` field. ``last`` fields end in \\n, others in a space."""
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


# ---------------------------------------------------------------------------
# record parsing
# ---------------------------------------------------------------------------

def _parse_delimited(buf: bytes, i: int, typ: str, names: list[str]) -> tuple[dict, int]:
    """Parse a single-line, space-delimited record (I and E). No field has spaces."""
    nl = buf.find(b"\n", i)
    if nl == -1:
        raise _Torn("eof reading record line")
    line = buf[i:nl]
    parts = line.split(b" ")
    # parts[0] is the type char; one token per name follows.
    if len(parts) != len(names) + 1:
        raise _Corrupt(f"{typ} record has {len(parts) - 1} fields, want {len(names)}")
    rec: dict = {"type": typ}
    for name, raw in zip(names, parts[1:]):
        rec[name] = _text(raw)
    return rec, nl + 1


def _parse_S(buf: bytes, i: int) -> tuple[dict, int]:
    """Parse a length-prefixed S (command-start) record."""
    i += 2  # skip "S "
    seq, i = _token(buf, i)
    ts_ns, i = _token(buf, i)
    nonce, i = _token(buf, i)
    shlvl, i = _token(buf, i)
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
        "cmd": _text(cmd),
        "cwd": _text(cwd),
        "user": _text(user),
        "host": _text(host),
    }
    return rec, i


def _parse_one(buf: bytes, i: int) -> tuple[dict, int]:
    t = buf[i:i + 1]
    if t == b"S":
        if buf[i:i + 2] != b"S ":
            raise _Torn("eof after type char")
        return _parse_S(buf, i)
    if t == b"E":
        return _parse_delimited(buf, i, "E", ["seq", "ts_ns", "nonce", "exit"])
    if t == b"I":
        return _parse_delimited(buf, i, "I", ["nonce", "ts_ns", "pid", "shell"])
    raise _Corrupt(f"unknown record type {t!r}")


def parse(buf: bytes) -> tuple[list[dict], list[dict]]:
    """Parse a whole ``hooks.rec`` buffer.

    Returns ``(records, warnings)``. ``records`` is the list of I/S/E dicts in
    file order; ``warnings`` lists any torn/corrupt spans with byte offsets, so
    the manifest can record them (§9).
    """
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


# ---------------------------------------------------------------------------
# event emission
# ---------------------------------------------------------------------------

def _int(s: str) -> int:
    return int(s)


def _safe(s: str) -> str:
    """Make a surrogateescape string safe to JSON-encode with ensure_ascii=False.

    Authoritative bytes always live in hooks.rec (hashed in the manifest); the
    rare non-UTF-8 byte becomes U+FFFD here so events.jsonl stays valid JSON.
    """
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def to_events(records: list[dict]) -> Iterator[dict]:
    """Translate parsed records into events.jsonl event dicts, in file order."""
    for r in records:
        t = r["type"]
        if t == "I":
            yield {
                "event": "init",
                "nonce": r["nonce"],
                "ts_ns": _int(r["ts_ns"]),
                "pid": _int(r["pid"]),
                "shell": r["shell"],
            }
        elif t == "S":
            yield {
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
        elif t == "E":
            yield {
                "event": "cmd_end",
                "seq": _int(r["seq"]),
                "ts_ns": _int(r["ts_ns"]),
                "nonce": r["nonce"],
                "exit": _int(r["exit"]),
            }


def events_from_file(path: str | Path) -> tuple[list[dict], list[dict]]:
    """Read a hooks.rec file and return (events, warnings)."""
    buf = Path(path).read_bytes()
    records, warnings = parse(buf)
    return list(to_events(records)), warnings


def write_jsonl(events: Iterator[dict], out) -> None:
    for ev in events:
        out.write(json.dumps(ev, ensure_ascii=False, sort_keys=False))
        out.write("\n")


# ---------------------------------------------------------------------------
# CLI + self-test
# ---------------------------------------------------------------------------

def _selftest() -> int:
    # Covers: init, a command with spaces + colon + embedded newline (heredoc),
    # its end record, and a torn final record (truncated mid-value).
    good = (
        b"I abc123 1700000000000000000 4242 bash\n"
        b"S 1 1700000000111111000 abc123 1 "
        b"32:cat <<EOF\nline: with: colons\nEOF 5:/root 4:root 3:kal\n"
        b"E 1 1700000000222222000 abc123 0\n"
    )
    torn = b"S 2 1700000000333333000 abc123 1 99:too short"
    records, warnings = parse(good + torn)
    assert len(records) == 3, records
    assert warnings and warnings[0]["warning"] == "torn_record", warnings
    evs = list(to_events(records))
    assert evs[0]["event"] == "init" and evs[0]["pid"] == 4242
    assert evs[1]["event"] == "cmd_start"
    assert evs[1]["cmd"] == "cat <<EOF\nline: with: colons\nEOF"
    assert evs[1]["cwd"] == "/root" and evs[1]["user"] == "root" and evs[1]["host"] == "kal"
    assert evs[2]["event"] == "cmd_end" and evs[2]["exit"] == 0
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
    return 0  # a torn last line is normal, not a failure


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
