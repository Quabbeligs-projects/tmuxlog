import json
import sys
from pathlib import Path


class _Torn(Exception):
    pass

class _Corrupt(Exception):
    pass


def _token(buf, i):
    sp = buf.find(b" ", i)
    if sp < 0:
        raise _Torn("eof reading token")
    return buf[i:sp], sp + 1


def _is_lenfield(buf, i):
    colon = buf.find(b":", i)
    if colon == -1:
        return False
    space = buf.find(b" ", i)
    if space != -1 and space < colon:
        return False
    return buf[i:colon].isdigit()


def _lenfield(buf, i, last):
    colon = buf.find(b":", i)
    if colon == -1:
        raise _Torn("eof reading length prefix")
    ln = buf[i:colon]
    if not ln.isdigit():
        raise _Corrupt(f"bad length prefix {ln!r}")
    start = colon + 1
    end = start + int(ln)
    if end > len(buf):
        raise _Torn("eof reading value")
    sep = buf[end:end+1]
    want = b"\n" if last else b" "
    if sep != want:
        if not sep:
            raise _Torn("eof reading field separator")
        raise _Corrupt(f"expected {want!r} after length field, got {sep!r}")
    return buf[start:end], end + 1


def txt(b):
    return b.decode("utf-8", "surrogateescape")


def _line_fields(buf, i, kind):
    nl = buf.find(b"\n", i)
    if nl == -1:
        raise _Torn(f"eof reading {kind} record")
    return [txt(x) for x in buf[i:nl].split(b" ")[1:]], nl + 1


def _parse_I(buf, i):
    f, nxt = _line_fields(buf, i, "I")
    if not 4 <= len(f) <= 6:
        raise _Corrupt(f"I record has {len(f)} fields, want 4-6")
    rec = {"type": "I", "nonce": f[0], "ts_ns": f[1], "pid": f[2], "shell": f[3]}
    if len(f) > 4:
        rec["shell_version"] = f[4]
    if len(f) > 5:
        rec["shlvl"] = f[5]
    return rec, nxt


def _parse_E(buf, i):
    f, nxt = _line_fields(buf, i, "E")
    if len(f) != 4:
        raise _Corrupt(f"E record has {len(f)} fields, want 4")
    return {"type": "E", "seq": f[0], "ts_ns": f[1], "nonce": f[2], "exit": f[3]}, nxt


def _parse_S(buf, i):
    i += 2
    seq, i = _token(buf, i)
    ts, i = _token(buf, i)
    nonce, i = _token(buf, i)
    shlvl, i = _token(buf, i)
    src = None
    if not _is_lenfield(buf, i):
        s, i = _token(buf, i)
        src = txt(s)
    cmd, i = _lenfield(buf, i, False)
    cwd, i = _lenfield(buf, i, False)
    user, i = _lenfield(buf, i, False)
    host, i = _lenfield(buf, i, True)
    return {
        "type": "S",
        "seq": txt(seq), "ts_ns": txt(ts), "nonce": txt(nonce), "shlvl": txt(shlvl),
        "src": src,
        "cmd": txt(cmd), "cwd": txt(cwd), "user": txt(user), "host": txt(host),
    }, i


def _parse_T(buf, i):
    i += 2
    seq, i = _token(buf, i)
    ts, i = _token(buf, i)
    nonce, i = _token(buf, i)
    part, i = _token(buf, i)
    cmd, i = _lenfield(buf, i, True)
    return {"type": "T", "seq": txt(seq), "ts_ns": txt(ts), "nonce": txt(nonce),
            "part": txt(part), "cmd": txt(cmd)}, i


def _parse_one(buf, i):
    t = buf[i:i+1]
    if t == b"S" or t == b"T":
        if buf[i+1:i+2] != b" ":
            raise _Torn("eof after type char")
        if t == b"S":
            return _parse_S(buf, i)
        return _parse_T(buf, i)
    elif t == b"E":
        return _parse_E(buf, i)
    elif t == b"I":
        return _parse_I(buf, i)
    raise _Corrupt(f"unknown record type {t!r}")


def parse(buf):
    records = []
    warnings = []
    i = 0
    n = len(buf)
    while i < n:
        start = i
        try:
            rec, i = _parse_one(buf, i)
        except _Torn as e:
            warnings.append({"warning": "torn_record", "offset": start, "bytes": n - start, "detail": str(e)})
            break
        except _Corrupt as e:
            nl = buf.find(b"\n", i)
            if nl == -1:
                warnings.append({"warning": "corrupt_record", "offset": start, "bytes": n - start, "detail": str(e)})
                break
            warnings.append({"warning": "corrupt_record", "offset": start, "bytes": nl + 1 - start, "detail": str(e)})
            i = nl + 1
            continue
        records.append(rec)
    return records, warnings


def _safe(s):
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def to_events(records):
    for r in records:
        t = r["type"]
        if t == "I":
            ev = {"event": "init", "nonce": r["nonce"], "ts_ns": int(r["ts_ns"]),
                  "pid": int(r["pid"]), "shell": r["shell"]}
            if "shell_version" in r:
                ev["shell_version"] = r["shell_version"]
            if "shlvl" in r:
                ev["shlvl"] = int(r["shlvl"])
            yield ev
        elif t == "S":
            ev = {"event": "cmd_start", "seq": int(r["seq"]), "ts_ns": int(r["ts_ns"]),
                  "nonce": r["nonce"], "depth": int(r["shlvl"])}
            for k in ("cmd", "cwd", "user", "host"):
                ev[k] = _safe(r[k])
            if r.get("src") is not None:
                ev["src"] = r["src"]
            yield ev
        elif t == "T":
            yield {"event": "cmd_part", "seq": int(r["seq"]), "ts_ns": int(r["ts_ns"]),
                   "nonce": r["nonce"], "part": int(r["part"]), "cmd": _safe(r["cmd"])}
        elif t == "E":
            yield {"event": "cmd_end", "seq": int(r["seq"]), "ts_ns": int(r["ts_ns"]),
                   "nonce": r["nonce"], "exit": int(r["exit"])}


def events_from_file(path):
    records, warnings = parse(Path(path).read_bytes())
    return list(to_events(records)), warnings


def write_jsonl(events, out):
    for ev in events:
        out.write(json.dumps(ev, ensure_ascii=False))
        out.write("\n")


TEST_DATA = (
    b"I abc123 1700000000000000000 4242 bash 5.2.15 1\n"
    b"S 1 1700000000111111000 abc123 1 bc "
    b"32:cat <<EOF\nline: with: colons\nEOF 5:/root 4:root 3:kal\n"
    b"T 1 1700000000111222000 abc123 1 3:cat\n"
    b"E 1 1700000000222222000 abc123 0\n"
    b"S 2 1700000000333333000 abc123 1 10: nmap -sV  5:/root 4:root 3:kal\n"
    b"E 2 1700000000444444000 abc123 0\n"
)


def selftest():
    records, warnings = parse(TEST_DATA + b"S 3 1700000000555555000 abc123 1 bc 99:too short")
    assert len(records) == 6, [r["type"] for r in records]
    assert warnings and warnings[0]["warning"] == "torn_record", warnings
    evs = list(to_events(records))
    kinds = [e["event"] for e in evs]
    assert kinds == ["init", "cmd_start", "cmd_part", "cmd_end", "cmd_start", "cmd_end"], kinds
    assert evs[0]["shell_version"] == "5.2.15" and evs[0]["shlvl"] == 1
    assert evs[1]["src"] == "bc"
    assert evs[1]["cmd"] == "cat <<EOF\nline: with: colons\nEOF"
    assert evs[2]["event"] == "cmd_part" and evs[2]["part"] == 1 and evs[2]["cmd"] == "cat"
    assert evs[4]["cmd"] == " nmap -sV " and "src" not in evs[4]
    assert evs[5]["exit"] == 0
    print("selftest OK:", len(records), "records,", len(warnings), "warning(s)")
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m tmuxlog.journal <hooks.rec>    (jsonl goes to stdout)\n"
              "       python -m tmuxlog.journal --selftest", file=sys.stderr)
        return 0 if argv else 2
    if argv[0] == "--selftest":
        return selftest()
    events, warnings = events_from_file(argv[0])
    write_jsonl(iter(events), sys.stdout)
    for w in warnings:
        print("warning:", json.dumps(w), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
