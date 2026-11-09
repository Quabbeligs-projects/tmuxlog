import bisect
import json
from pathlib import Path

from tmuxlog import ansi, journal
from tmuxlog.capture import read_arrivals

OSC_PREFIX = b"\x1b]6973;tmuxlog;"
SENT_PREFIX = b"@@TMUXLOG;"
SENT_CLOSE = b"@@"


def _mark(payload, start, end):
    f = payload.split(b";")

    def field(i, enc="ascii", err="replace"):
        return f[i].decode(enc, err) if len(f) > i else None

    return {
        "start": start,
        "end": end,
        "kind": f[0].decode("ascii", "replace") if f and f[0] else "",
        "nonce": field(1, "utf-8", "surrogateescape"),
        "seq": field(2),
        "exit": field(3),
    }


def _osc_end(raw, ps):
    k = ps
    n = len(raw)
    while k < n:
        if raw[k] == 0x07:
            return raw[ps:k], k + 1
        if raw[k] == 0x1b and k + 1 < n and raw[k+1] == 0x5c:
            return raw[ps:k], k + 2
        k += 1
    return None, None


def _skip_csi(raw, i, final):
    if raw[i:i+2] != b"\x1b[":
        return i
    k = i + 2
    while k < len(raw) and 0x30 <= raw[k] <= 0x3f:
        k += 1
    if k < len(raw) and raw[k] == final:
        return k + 1
    return i


def _scan_marks(raw):
    marks = []
    unterminated = 0
    i = 0
    while i < len(raw):
        o = raw.find(OSC_PREFIX, i)
        s = raw.find(SENT_PREFIX, i)
        if o == -1 and s == -1:
            break
        if s == -1 or (o != -1 and o < s):
            payload, end = _osc_end(raw, o + len(OSC_PREFIX))
            if end is None:
                unterminated += 1
                break
            marks.append(_mark(payload, o, end))
        else:
            ps = s + len(SENT_PREFIX)
            close = raw.find(SENT_CLOSE, ps)
            if close == -1:
                unterminated += 1
                break
            end = _skip_csi(raw, close + 2, ord("D"))
            end = _skip_csi(raw, end, ord("K"))
            marks.append(_mark(raw[ps:close], s, end))
        i = end
    return marks, unterminated


def _echo_matches(region, cmd):
    if not cmd:
        return False
    want = cmd.replace("\r", "").strip()
    if not want:
        return False
    got = ansi.strip(region).decode("utf-8", "surrogateescape").replace("\r", "").strip()
    return got == want


def split(pane_dir, sealed=None, gap_ms=500):
    d = Path(pane_dir)

    def read(name):
        return (d / name).read_bytes() if (d / name).exists() else b""

    raw = read("raw.bin")
    hooks = read("hooks.rec")
    arrivals = read_arrivals(d) if (d / "arrivals.idx").exists() else []
    if sealed is None:
        sealed = (d / "sealed").exists()

    records, hook_warnings = journal.parse(hooks)
    nonces = set(r["nonce"] for r in records if r.get("nonce"))
    S, E, T, I = {}, {}, {}, {}
    for r in records:
        if r["type"] == "I":
            I[r["nonce"]] = r
            continue
        key = (r["nonce"], r["seq"])
        if r["type"] == "S":
            S[key] = r
        elif r["type"] == "E":
            E[key] = r
        elif r["type"] == "T":
            T.setdefault(key, []).append(r)

    found, unterminated = _scan_marks(raw)
    marks = [m for m in found if m["nonce"] in nonces]
    rejected = len(found) - len(marks)

    arrivals = sorted(arrivals, key=lambda a: a[1])
    offs = [a[1] for a in arrivals]
    stamps = [a[0] for a in arrivals]

    def when(offset):
        if not offs:
            return None
        i = bisect.bisect_right(offs, offset) - 1
        if i < 0:
            i = 0
        return stamps[i]

    blocks = []
    warnings = list(hook_warnings)
    if rejected:
        warnings.append({"warning": "rejected_marks", "count": rejected})
    if unterminated:
        warnings.append({"warning": "unterminated_mark", "count": unterminated})

    def region(kind, a, b):
        if b <= a:
            return
        body = raw[a:b]
        blocks.append({
            "kind": kind,
            "start_ns": when(a), "end_ns": when(b - 1),
            "raw_offsets": [a, b], "bytes_total": b - a,
            "raw": body, "clean": ansi.strip(body),
        })

    def orphans(a, b):
        cuts = [a]
        if gap_ms is not None and gap_ms >= 0:
            lo = bisect.bisect_right(offs, a)
            hi = bisect.bisect_left(offs, b)
            for i in range(max(lo, 1), hi):
                if stamps[i] - stamps[i-1] > gap_ms * 1_000_000:
                    cuts.append(offs[i])
        cuts.append(b)
        for x, y in zip(cuts, cuts[1:]):
            region("orphan", x, y)

    if marks:
        region("preamble", 0, marks[0]["start"])
    else:
        region("preamble", 0, len(raw))

    echo = {}
    for idx, m in enumerate(marks):
        nxt = None
        if idx + 1 < len(marks):
            nxt = marks[idx + 1]
        a = m["end"]
        b = nxt["start"] if nxt else len(raw)

        if m["kind"] == "B":
            if nxt and nxt["kind"] == "C":
                key = (nxt["nonce"], nxt["seq"])
                cmd = S.get(key, {}).get("cmd")
                if _echo_matches(raw[a:b], cmd):
                    echo[key] = "matched"
                    continue
                echo[key] = "kept"
            region("orphan", a, b)

        elif m["kind"] == "C":
            key = (m["nonce"], m["seq"])
            s = S.get(key)
            start_ns = int(s["ts_ns"]) if s else when(a)
            closed = nxt is not None and nxt["kind"] == "D" and nxt["nonce"] == m["nonce"] and nxt["seq"] == m["seq"]
            if closed:
                e = E.get(key)
                if nxt["exit"] is not None:
                    exit_code = int(nxt["exit"])
                elif e:
                    exit_code = int(e["exit"])
                else:
                    exit_code = None
                end_ns = int(e["ts_ns"]) if e else when(b - 1)
                status = "interrupted" if exit_code == 130 else "complete"
            else:
                exit_code = None
                end_ns = None
                status = "unterminated" if sealed else "still_running"

            parts = sorted(T.get(key, []), key=lambda r: int(r["part"]))
            info = I.get(key[0], {})
            body = raw[a:b]
            blocks.append({
                "kind": "command", "nonce": m["nonce"], "seq": m["seq"],
                "start_ns": start_ns, "end_ns": end_ns,
                "exit_code": exit_code, "status": status,
                "echo_dedup": echo.get(key),
                "raw_offsets": [a, b], "bytes_total": b - a,
                "raw": body, "clean": ansi.strip(body),
                "cmd": s["cmd"] if s else None,
                "cmd_parts": [p["cmd"] for p in parts],
                "cwd": s["cwd"] if s else None,
                "user": s["user"] if s else None,
                "host": s["host"] if s else None,
                "depth": int(s["shlvl"]) if s and s.get("shlvl") else None,
                "shell": info.get("shell"),
                "shell_version": info.get("shell_version"),
            })

        elif m["kind"] == "D":
            orphans(a, b)

    return {"blocks": blocks, "warnings": warnings}


def sealed_summary(pane_dir):
    p = Path(pane_dir) / "sealed"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
