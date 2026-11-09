import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tmuxlog import ansi, capture, journal, split

CAP_DEFAULT = 100 * 1024 * 1024


def sha(b):
    return hashlib.sha256(b).hexdigest()


def iso(ns):
    if ns is None:
        return "-"
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat()


def _sort_key(b):
    meta = b["_meta"]
    ns = b["start_ns"]
    if ns is None:
        if b["kind"] == "preamble":
            ns = meta.get("created_ns")
        else:
            ns = b["end_ns"]
        ns = ns or 0
    try:
        num = int(meta.get("pane_num", 0) or 0)
    except (TypeError, ValueError):
        num = 0
    return ns, num, meta.get("session", "")


def _header(bid, b, raw_sha):
    m = b["_meta"]
    where = "%s:%s.%s" % (m.get("session", "?"), m.get("window_index", "?"), m.get("pane_index", "?"))
    lines = [f"===== BLOCK {bid} [{b['kind']}] pane {b['_pane']} ({m.get('pane_id', '?')} {where}) ====="]
    times = f"start={iso(b['start_ns'])} ({b['start_ns']})  end={iso(b['end_ns'])} ({b['end_ns']})"
    if b["kind"] == "command":
        cmd = b.get("cmd") or ""
        if b.get("cmd_parts"):
            cmd = cmd + " | " + " | ".join(b["cmd_parts"]) + "   (stages reconstructed)"
        lines.append("cmd: " + cmd)
        lines.append(f"cwd={b.get('cwd')} user={b.get('user')} host={b.get('host')} "
                     f"shell={b.get('shell')} depth={b.get('depth')}")
        lines.append(f"{times}  exit={b['exit_code']}  status={b['status']}")
        if b.get("echo_dedup"):
            lines.append("echo=" + b["echo_dedup"])
    else:
        lines.append(times)
    lines.append(f"sha256(raw)={raw_sha}  bytes={b['bytes_total']}")
    lines.append("----- output -----")
    return ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")


def write_file(path, data):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        pos = 0
        while pos < len(view):
            pos += os.write(fd, view[pos:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def collect_blocks(session_dir):
    panes_dir = Path(session_dir) / "panes"
    blocks = []
    events = []
    gaps = []
    warnings = []
    if not panes_dir.is_dir():
        return {"blocks": blocks, "events": events, "gaps": gaps, "warnings": warnings}

    for pane in sorted(panes_dir.iterdir()):
        has_rec = (pane / "hooks.rec").exists()
        if not has_rec and not (pane / "raw.bin").exists():
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
        for w in res["warnings"]:
            warnings.append(dict(w, pane=pane.name))
        if has_rec:
            evs = journal.events_from_file(pane / "hooks.rec")[0]
            events.extend(dict(e, pane=pane.name) for e in evs)
        for g in capture.read_gaps(pane):
            gaps.append(dict(g, pane=pane.name))

    blocks.sort(key=_sort_key)
    events.sort(key=lambda e: (e.get("ts_ns", 0), e.get("pane", "")))
    return {"blocks": blocks, "events": events, "gaps": gaps, "warnings": warnings}


def finalize(session_dir, cap_bytes=CAP_DEFAULT):
    session_dir = Path(session_dir)
    stuff = collect_blocks(session_dir)
    blocks = stuff["blocks"]
    events = stuff["events"]
    gaps = stuff["gaps"]
    warnings = stuff["warnings"]

    master = bytearray()
    mblocks = []
    gap_bytes = sum(g.get("bytes", 0) for g in gaps)
    if gaps:
        master += (f"[tmuxlog: WARNING: {len(gaps)} capture gap(s) covering {gap_bytes} byte(s); "
                   f"output is INCOMPLETE. See manifest.json.]\n\n").encode()

    bid = 0
    for b in blocks:
        bid += 1
        raw = b["raw"]
        raw_sha = sha(raw)
        uid = b["_pane"]
        truncated = len(raw) > cap_bytes
        if truncated:
            half = cap_bytes // 2
            lo, hi = b["raw_offsets"]
            note = (f"\n[tmuxlog: TRUNCATED, showing {2 * half} of {len(raw)} bytes; "
                    f"full block = panes/{uid}/raw.bin [{lo},{hi}) sha256={raw_sha}]\n").encode()
            body = raw[:half] + note + raw[-half:]
        else:
            body = raw
        master += _header(bid, b, raw_sha) + body + b"\n" + f"===== END BLOCK {bid} =====\n\n".encode()

        mblocks.append({
            "block_id": bid,
            "kind": b["kind"],
            "pane_uid": uid,
            "pane_id": b["_meta"].get("pane_id"),
            "session": b["_meta"].get("session"),
            "start_ns": b["start_ns"],
            "end_ns": b["end_ns"],
            "exit_code": b.get("exit_code"),
            "status": b.get("status"),
            "cmd": b.get("cmd"),
            "cmd_parts": b.get("cmd_parts"),
            "cwd": b.get("cwd"),
            "user": b.get("user"),
            "host": b.get("host"),
            "shell": b.get("shell"),
            "depth": b.get("depth"),
            "echo_dedup": b.get("echo_dedup"),
            "raw_offsets": b["raw_offsets"],
            "bytes_total": b["bytes_total"],
            "truncated": truncated,
            "sha256_raw": raw_sha,
            "sha256_clean": sha(b["clean"]),
        })

    ev_lines = [json.dumps(e, ensure_ascii=False) for e in events]
    ev_lines += [json.dumps({"event": "block", **mb}, ensure_ascii=False) for mb in mblocks]
    events_text = "".join(l + "\n" for l in ev_lines)

    final = session_dir / "final"
    final.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(final, 0o700)
    except OSError:
        pass

    master = bytes(master)
    clean = ansi.strip(master)
    ev_bytes = events_text.encode("utf-8", "surrogateescape")
    write_file(final / "master.log", master)
    write_file(final / "master.clean.log", clean)
    write_file(final / "events.jsonl", ev_bytes)

    manifest = {
        "session": session_dir.name,
        "finalized_ns": __import__("time").time_ns(),
        "block_count": len(mblocks),
        "blocks": mblocks,
        "gaps": gaps,
        "gap_bytes": gap_bytes,
        "warnings": warnings,
        "files": {
            "master.log": {"bytes": len(master), "sha256": sha(master)},
            "master.clean.log": {"bytes": len(clean), "sha256": sha(clean)},
            "events.jsonl": {"bytes": len(ev_bytes), "sha256": sha(ev_bytes)},
        },
    }
    write_file(final / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return {"dir": str(final), "blocks": len(mblocks), "gaps": len(gaps), "warnings": len(warnings)}
