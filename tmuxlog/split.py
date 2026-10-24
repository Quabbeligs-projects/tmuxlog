"""Split a pane's raw.bin into ordered blocks — a pure function of the pane files.

Reads raw.bin, hooks.rec, arrivals.idx (and `sealed` if present) and returns the
blocks for that pane. finalize (Phase 3) adds pane/session metadata, sha256,
truncation and cross-pane ordering; this module only does the segmentation, which
is the subtle part.

Boundary marks (osc mode) live in raw.bin as the private OSC
``ESC ] 6973 ; tmuxlog ; <KIND> ; <nonce> [; <seq> [; <exit>]] (BEL | ST)``
emitted by the shell hooks (docs/DESIGN.md §2):

    I  init (a nonce registered)        A  prompt start
    B  prompt end (embedded in PS1)     C  pre-exec, the Enter moment
    D  command done

A mark is honoured only if its nonce appears in hooks.rec; a mark with an
unknown nonce (a replayed or `cat`-ed old log, injected output) is NOT a
boundary — it stays inside whatever block contains it. Segmentation, per §4:

    before the first mark                     -> preamble block
    region opened by A (A..B)                 -> prompt, dropped
    region opened by B (B..C)                 -> typed echo: compared with the
                                                 command; matched -> dropped,
                                                 else -> orphan
    region opened by C (C..D)                 -> command block body
    region opened by D (D..next)              -> orphan (background output etc.)
    region opened by I                        -> setup, dropped

Command metadata (cmd, cwd, user, host, shell, depth, timestamps, exit) comes
from the matching hooks.rec records, not the marks. A command whose D mark is
missing (pane killed, shell exited) is left open: still_running if the pane is
not sealed, else unterminated. Orphan and preamble timestamps come from
arrivals.idx.

stdlib only; uses tmuxlog.journal, tmuxlog.ansi and tmuxlog.capture.read_arrivals.
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path

from tmuxlog import ansi, journal
from tmuxlog.capture import read_arrivals

MARK_PREFIX = b"\x1b]6973;tmuxlog;"
BEL = 0x07
ESC = 0x1B


# ---------------------------------------------------------------------------
# mark scanning
# ---------------------------------------------------------------------------

def _scan_marks(raw: bytes) -> tuple[list[dict], int]:
    """Find every private OSC mark. Returns (marks, unterminated_count)."""
    marks, unterminated = [], 0
    i = 0
    while True:
        j = raw.find(MARK_PREFIX, i)
        if j == -1:
            break
        ps = j + len(MARK_PREFIX)
        k, end, payload = ps, None, None
        while k < len(raw):
            if raw[k] == BEL:
                payload, end = raw[ps:k], k + 1
                break
            if raw[k] == ESC and k + 1 < len(raw) and raw[k + 1] == 0x5C:  # ST
                payload, end = raw[ps:k], k + 2
                break
            k += 1
        if end is None:            # unterminated mark at EOF: not a boundary
            unterminated += 1
            break
        f = payload.split(b";")
        marks.append({
            "start": j, "end": end,
            "kind": f[0].decode("ascii", "replace") if f and f[0] else "",
            "nonce": f[1].decode("utf-8", "surrogateescape") if len(f) > 1 else None,
            "seq": f[2].decode("ascii", "replace") if len(f) > 2 else None,
            "exit": f[3].decode("ascii", "replace") if len(f) > 3 else None,
        })
        i = end
    return marks, unterminated


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _norm(data: bytes) -> str:
    """ANSI-stripped, CR-removed, end-trimmed text for echo comparison."""
    s = ansi.strip(data).decode("utf-8", "surrogateescape")
    return s.replace("\r", "").strip()


def _echo_matches(region: bytes, cmd: str | None) -> bool:
    # The echo region is dropped only when it IS the echoed command line (after
    # ANSI/CR strip), not when the command merely appears inside it — otherwise
    # stray output printed at the prompt would be swallowed. A multi-line echo
    # (heredoc, continuation prompts) won't match and becomes an orphan, which
    # is the documented behaviour.
    if not cmd:
        return False
    nc = cmd.replace("\r", "").strip()
    return bool(nc) and _norm(region) == nc


class _Clock:
    """Map a raw.bin offset to the arrival time of the read that delivered it."""

    def __init__(self, arrivals: list[tuple[int, int]]):
        arrivals = sorted(arrivals, key=lambda a: a[1])
        self._offs = [off for _, off in arrivals]
        self._ts = [ts for ts, _ in arrivals]

    def at(self, offset: int) -> int | None:
        if not self._offs:
            return None
        i = bisect.bisect_right(self._offs, offset) - 1
        return self._ts[i] if i >= 0 else self._ts[0]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def split(pane_dir, sealed: bool | None = None) -> dict:
    """Return {"blocks": [...], "warnings": [...]} for one pane directory."""
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

    # preamble: everything before the first honoured mark
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
            # typed echo up to the next command's C mark
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
            # output arriving after the command finished, before the next prompt
            blk = region_block("orphan", a, b)
            if blk:
                blocks.append(blk)

        # kind A or I: prompt / setup region, dropped

    return {"blocks": blocks, "warnings": warnings}


def sealed_summary(pane_dir) -> dict | None:
    p = Path(pane_dir) / "sealed"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
