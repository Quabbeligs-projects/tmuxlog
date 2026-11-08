from __future__ import annotations

import bisect
import json
from pathlib import Path

from tmuxlog import ansi, journal
from tmuxlog.capture import read_arrivals

OSC_PREFIX = b"\x1b]6973;tmuxlog;"
SENT_PREFIX = b"@@TMUXLOG;"
SENT_CLOSE = b"@@"
BEL = 0x07
ESC = 0x1B

# mark scanning
def _mk(payload: bytes, start: int, end: int) -> dict:
    f = payload.split(b";")
    return {
        "start": start, "end": end,
        "kind": f[0].decode("ascii", "replace") if f and f[0] else "",
        "nonce": f[1].decode("utf-8", "surrogateescape") if len(f) > 1 else None,
        "seq": f[2].decode("ascii", "replace") if len(f) > 2 else None,
        "exit": f[3].decode("ascii", "replace") if len(f) > 3 else None,
    }


def _osc_payload_end(raw: bytes, ps: int):
    k = ps
    while k < len(raw):
        if raw[k] == BEL:
            return raw[ps:k], k + 1
        if raw[k] == ESC and k + 1 < len(raw) and raw[k + 1] == 0x5C:  # ST
            return raw[ps:k], k + 2
        k += 1
    return None, None


def _consume_csi(raw: bytes, i: int, final: int) -> int:
    if raw[i:i + 2] != b"\x1b[":
        return i
    k = i + 2
    while k < len(raw) and 0x30 <= raw[k] <= 0x3F:
        k += 1
    return k + 1 if k < len(raw) and raw[k] == final else i

def _scan_marks(raw: bytes) -> tuple[list[dict], int]:
    """Find every boundary mark (osc or sentinel). Returns (marks, unterminated)."""
    marks, unterminated = [], 0
    i, n = 0, len(raw)
    while i < n:
        o = raw.find(OSC_PREFIX, i)
        s = raw.find(SENT_PREFIX, i)
        if o == -1 and s == -1:
            break
        if s == -1 or (o != -1 and o < s):           # OSC comes first
            payload, end = _osc_payload_end(raw, o + len(OSC_PREFIX))
            if end is None:                           # unterminated at EOF
                unterminated += 1
                break
            marks.append(_mk(payload, o, end))
            i = end
        else:                                         # sentinel comes first
            ps = s + len(SENT_PREFIX)
            close = raw.find(SENT_CLOSE, ps)
            if close == -1:                           # unterminated at EOF
                unterminated += 1
                break
            end = close + len(SENT_CLOSE)
            end = _consume_csi(raw, end, ord("D"))
            end = _consume_csi(raw, end, ord("K"))
            marks.append(_mk(raw[ps:close], s, end))
            i = end
    return marks, unterminated

# helpers
def _norm(data: bytes) -> str:
    """ANSI-stripped, CR-removed, end-trimmed text for echo comparison."""
    s = ansi.strip(data).decode("utf-8", "surrogateescape")
    return s.replace("\r", "").strip()

def _echo_matches(region: bytes, cmd: str | None) -> bool:
    if not cmd:
        return False
    nc = cmd.replace("\r", "").strip()
    return bool(nc) and _norm(region) == nc

class _Clock:
    def __init__(self, arrivals: list[tuple[int, int]]):
        arrivals = sorted(arrivals, key=lambda a: a[1])
        self._offs = [off for _, off in arrivals]
        self._ts = [ts for ts, _ in arrivals]

    def at(self, offset: int) -> int | None:
        if not self._offs:
            return None
        i = bisect.bisect_right(self._offs, offset) - 1
        return self._ts[i] if i >= 0 else self._ts[0]
        
# main
def split(pane_dir, sealed: bool | None = None) -> dict:
    d = Path(pane_dir)
    raw = (d / "raw.bin").read_bytes() if (d / "raw.bin").exists() else b""
    hooks = (d / "hooks.rec").read_bytes() if (d / "hooks.rec").exists() else b""
    arrivals = read_arrivals(d) if (d / "arrivals.idx").exists() else []
    if sealed is None:
        sealed = (d / "sealed").exists()

    records, hook_warnings = journal.parse(hooks)
    valid_nonces = {r["nonce"] for r in records if r.get("nonce")}
    S = {(r["nonce"], r["seq"]): r for r in records if r["type"] == "S"}
    E = {(r["nonce"], r["seq"]): r for r in records if r["type"] == "E"}
    T: dict[tuple, list] = {}
    for r in records:
        if r["type"] == "T":
            T.setdefault((r["nonce"], r["seq"]), []).append(r)
    I = {r["nonce"]: r for r in records if r["type"] == "I"}

    all_marks, unterminated = _scan_marks(raw)
    rejected = [m for m in all_marks if m["nonce"] not in valid_nonces]
    marks = [m for m in all_marks if m["nonce"] in valid_nonces]

    clock = _Clock(arrivals)
    blocks: list[dict] = []
    warnings: list[dict] = list(hook_warnings)
    if rejected:
        warnings.append({"warning": "rejected_marks", "count": len(rejected)})
    if unterminated:
        warnings.append({"warning": "unterminated_mark", "count": unterminated})

    def region_block(kind: str, a: int, b: int) -> dict | None:
        if b <= a:
            return None
        body = raw[a:b]
        return {
            "kind": kind,
            "start_ns": clock.at(a), "end_ns": clock.at(b - 1),
            "raw_offsets": [a, b], "bytes_total": b - a,
            "raw": body, "clean": ansi.strip(body),
        }

    def cmd_fields(key):
        s = S.get(key)
        parts = [p["cmd"] for p in sorted(T.get(key, []), key=lambda r: int(r["part"]))]
        info = I.get(key[0], {})
        return {
            "cmd": s["cmd"] if s else None,
            "cmd_parts": parts,
            "cwd": s["cwd"] if s else None,
            "user": s["user"] if s else None,
            "host": s["host"] if s else None,
            "depth": int(s["shlvl"]) if s and s.get("shlvl") else None,
            "shell": info.get("shell"),
            "shell_version": info.get("shell_version"),
        }

    # preamble
    first = marks[0]["start"] if marks else len(raw)
    pre = region_block("preamble", 0, first)
    if pre:
        blocks.append(pre)

    echo = {}  # (nonce, seq) -> "matched" | "kept"
    for idx, m in enumerate(marks):
        nxt = marks[idx + 1] if idx + 1 < len(marks) else None
        a = m["end"]
        b = nxt["start"] if nxt else len(raw)
        kind = m["kind"]

        if kind == "B":
            if nxt and nxt["kind"] == "C":
                key = (nxt["nonce"], nxt["seq"])
                cmd = S.get(key, {}).get("cmd")
                if _echo_matches(raw[a:b], cmd):
                    echo[key] = "matched"
                else:
                    echo[key] = "kept"
                    blk = region_block("orphan", a, b)
                    if blk:
                        blocks.append(blk)
            else:
                blk = region_block("orphan", a, b)
                if blk:
                    blocks.append(blk)

        elif kind == "C":
            key = (m["nonce"], m["seq"])
            closed = bool(nxt and nxt["kind"] == "D"
                          and nxt["nonce"] == m["nonce"] and nxt["seq"] == m["seq"])
            body = raw[a:b]
            s = S.get(key)
            start_ns = int(s["ts_ns"]) if s else clock.at(a)
            if closed:
                e = E.get(key)
                exit_code = (int(nxt["exit"]) if nxt["exit"] is not None
                             else int(e["exit"]) if e else None)
                end_ns = int(e["ts_ns"]) if e else clock.at(b - 1)
                status = "interrupted" if exit_code == 130 else "complete"
            else:
                exit_code, end_ns = None, None
                status = "unterminated" if sealed else "still_running"
            block = {
                "kind": "command", "nonce": m["nonce"], "seq": m["seq"],
                "start_ns": start_ns, "end_ns": end_ns,
                "exit_code": exit_code, "status": status,
                "echo_dedup": echo.get(key),
                "raw_offsets": [a, b], "bytes_total": b - a,
                "raw": body, "clean": ansi.strip(body),
                **cmd_fields(key),
            }
            blocks.append(block)

        elif kind == "D":
            blk = region_block("orphan", a, b)
            if blk:
                blocks.append(blk)
    return {"blocks": blocks, "warnings": warnings}

def sealed_summary(pane_dir) -> dict | None:
    p = Path(pane_dir) / "sealed"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
