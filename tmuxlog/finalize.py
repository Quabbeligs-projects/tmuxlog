from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tmuxlog import ansi, capture, journal, split

CAP_DEFAULT = 100 * 1024 * 1024

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _iso(ns) -> str:
    if ns is None:
        return "-"
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat()

def _sort_key(b: dict):
    ns = b["start_ns"]
    if ns is None:
        ns = (b["_meta"].get("created_ns") if b["kind"] == "preamble" else b["end_ns"]) or 0
    try:
        pane_num = int(b["_meta"].get("pane_num", 0) or 0)
    except (TypeError, ValueError):
        pane_num = 0
    return (ns, pane_num, b["_meta"].get("session", ""))

def _truncate(body: bytes, cap: int, uid: str, offsets, sha: str) -> tuple[bytes, bool]:
    if len(body) <= cap:
        return body, False
    half = cap // 2
    a, b = offsets
    marker = (f"\n[tmuxlog: TRUNCATED — showing {2 * half} of {len(body)} bytes; "
              f"full block = panes/{uid}/raw.bin [{a},{b}) sha256={sha}]\n").encode()
    return body[:half] + marker + body[-half:], True

def _header(bid: int, b: dict, sha_raw: str) -> bytes:
    m = b["_meta"]
    loc = f"{m.get('session','?')}:{m.get('window_index','?')}.{m.get('pane_index','?')}"
    pane = f"{b['_pane']} ({m.get('pane_id','?')} {loc})"
    lines = [f"===== BLOCK {bid} [{b['kind']}] pane {pane} ====="]
    if b["kind"] == "command":
        cmd = b.get("cmd") or ""
        if b.get("cmd_parts"):
            cmd += " | " + " | ".join(b["cmd_parts"]) + "   (stages reconstructed)"
        lines.append(f"cmd: {cmd}")
        lines.append(f"cwd={b.get('cwd')} user={b.get('user')} host={b.get('host')} "
                     f"shell={b.get('shell')} depth={b.get('depth')}")
        lines.append(f"start={_iso(b['start_ns'])} ({b['start_ns']})  "
                     f"end={_iso(b['end_ns'])} ({b['end_ns']})  "
                     f"exit={b['exit_code']}  status={b['status']}")
        if b.get("echo_dedup"):
            lines.append(f"echo={b['echo_dedup']}")
    else:
        lines.append(f"start={_iso(b['start_ns'])} ({b['start_ns']})  "
                     f"end={_iso(b['end_ns'])} ({b['end_ns']})")
    lines.append(f"sha256(raw)={sha_raw}  bytes={b['bytes_total']}")
    lines.append("----- output -----")
    return ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape"

def _footer(bid: int) -> bytes:
    return f"===== END BLOCK {bid} =====\n\n".encode()

def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        mv = memoryview(data)
        off = 0
        while off < len(mv):                    
            off += os.write(fd, mv[off:])      
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)

def collect_blocks(session_dir) -> dict:
    session_dir = Path(session_dir)
    panes_dir = session_dir / "panes"
    blocks, events, gaps, warnings = [], [], [], []
    if panes_dir.is_dir():
        for pane in sorted(panes_dir.iterdir()):
            if not (pane / "raw.bin").exists() and not (pane / "hooks.rec").exists():
                continue
            meta = {}
            if (pane / "meta.json").exists():
                try:
                    meta = json.loads((pane / "meta.json").read_text())
                except (OSError, json.JSONDecodeError):
                    meta = {}
            res = split.split(pane)
            for blk in res["blocks"]:
                blk["_pane"] = pane.name
                blk["_meta"] = meta
                blocks.append(blk)
            warnings += [{**w, "pane": pane.name} for w in res["warnings"]]
            if (pane / "hooks.rec").exists():
                evs, _ = journal.events_from_file(pane / "hooks.rec")
                events += [{**e, "pane": pane.name} for e in evs]
            gaps += [{**g, "pane": pane.name} for g in capture.read_gaps(pane)]
    blocks.sort(key=_sort_key)
    events.sort(key=lambda e: (e.get("ts_ns", 0), e.get("pane", "")))
    return {"blocks": blocks, "events": events, "gaps": gaps, "warnings": warnings}

def finalize(session_dir, cap_bytes: int = CAP_DEFAULT) -> dict:
    session_dir = Path(session_dir)
    gathered = collect_blocks(session_dir)
    blocks, events = gathered["blocks"], gathered["events"]
    gaps, warnings = gathered["gaps"], gathered["warnings"]

    master = bytearray()
    manifest_blocks = []
    gap_bytes = sum(g.get("bytes", 0) for g in gaps)
    if gaps:
        banner = (f"[tmuxlog: WARNING — {len(gaps)} capture gap(s) covering "
                  f"{gap_bytes} byte(s); output is INCOMPLETE. See manifest.json.]\n\n").encode()
        master += banner

    for bid, b in enumerate(blocks, 1):
        sha_raw, sha_clean = _sha(b["raw"]), _sha(b["clean"])
        uid = b["_pane"]
        raw_body, trunc = _truncate(b["raw"], cap_bytes, uid, b["raw_offsets"], sha_raw)
        hdr = _header(bid, b, sha_raw)
        ftr = _footer(bid)
        master += hdr + raw_body + b"\n" + ftr
        manifest_blocks.append({
            "block_id": bid, "kind": b["kind"], "pane_uid": uid,
            "pane_id": b["_meta"].get("pane_id"), "session": b["_meta"].get("session"),
            "start_ns": b["start_ns"], "end_ns": b["end_ns"],
            "exit_code": b.get("exit_code"), "status": b.get("status"),
            "cmd": b.get("cmd"), "cmd_parts": b.get("cmd_parts"),
            "cwd": b.get("cwd"), "user": b.get("user"), "host": b.get("host"),
            "shell": b.get("shell"), "depth": b.get("depth"),
            "echo_dedup": b.get("echo_dedup"),
            "raw_offsets": b["raw_offsets"], "bytes_total": b["bytes_total"],
            "truncated": trunc, "sha256_raw": sha_raw, "sha256_clean": sha_clean,
        })

    events_text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    events_text += "".join(json.dumps({"event": "block", **mb}, ensure_ascii=False) + "\n"
                           for mb in manifest_blocks)

    final = session_dir / "final"
    final.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(final, 0o700)
    except OSError:
        pass

    master_bytes = bytes(master)
    clean_bytes = ansi.strip(master_bytes)
    events_bytes = events_text.encode("utf-8", "surrogateescape")
    _atomic_write(final / "master.log", master_bytes)
    _atomic_write(final / "master.clean.log", clean_bytes)
    _atomic_write(final / "events.jsonl", events_bytes)

    manifest = {
        "session": session_dir.name,
        "finalized_ns": __import__("time").time_ns(),
        "block_count": len(manifest_blocks),
        "blocks": manifest_blocks,
        "gaps": gaps, "gap_bytes": gap_bytes,
        "warnings": warnings,
        "files": {
            "master.log": {"bytes": len(master_bytes), "sha256": _sha(master_bytes)},
            "master.clean.log": {"bytes": len(clean_bytes), "sha256": _sha(clean_bytes)},
            "events.jsonl": {"bytes": len(events_bytes), "sha256": _sha(events_bytes)},
        },
    }
    _atomic_write(final / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return {"dir": str(final), "blocks": len(manifest_blocks),
            "gaps": len(gaps), "warnings": len(warnings)}
