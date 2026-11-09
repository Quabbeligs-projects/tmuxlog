import argparse
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tmuxlog import __version__, config, journal, paths


def _pane_dirs(session):
    d = session / "panes"
    if not d.is_dir():
        return []
    return sorted([p for p in d.iterdir() if (p / "hooks.rec").is_file()])


def _resolve_session(cfg, name):
    root = Path(cfg["paths"]["root"])
    if name:
        return paths.session_dir(root, name)
    sdir = root / "sessions"
    found = []
    if sdir.is_dir():
        found = sorted(sdir.iterdir(), key=lambda p: p.stat().st_mtime)
    if not found:
        raise SystemExit(f"tmuxlog: no sessions under {sdir}")
    return found[-1]


def _open_commands(events):
    running = {}
    for ev in events:
        k = (ev.get("nonce"), ev.get("seq"))
        if ev["event"] == "cmd_start":
            running[k] = ev
        elif ev["event"] == "cmd_end":
            running.pop(k, None)
    return list(running.values())


def session_events(session):
    events = []
    warnings = []
    for pane in _pane_dirs(session):
        evs, warns = journal.events_from_file(pane / "hooks.rec")
        for ev in evs:
            events.append({**ev, "pane": pane.name})
        for w in warns:
            warnings.append({**w, "pane": pane.name})
    events.sort(key=lambda e: (e["ts_ns"], e["pane"]))
    return events, warnings


def _tmux_pipes():
    try:
        pid = subprocess.run(["tmux", "display-message", "-p", "#{pid}"],
                             capture_output=True, text=True, timeout=5)
        ls = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pipe}"],
                            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None, {}
    if pid.returncode != 0 or ls.returncode != 0:
        return None, {}
    piped = {}
    for line in ls.stdout.splitlines():
        pane, _, flag = line.partition(" ")
        piped[pane] = (flag == "1")
    return int(pid.stdout.strip()), piped


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def warn_all(warnings):
    for w in warnings:
        print("tmuxlog: warning:", json.dumps(w), file=sys.stderr)


def cmd_events(args, cfg):
    rec = Path(args.target)
    if rec.is_dir():
        rec = rec / "hooks.rec"
    events, warnings = journal.events_from_file(rec)
    warn_all(warnings)
    if args.output == "-":
        journal.write_jsonl(iter(events), sys.stdout)
        return 0
    if args.output:
        out = Path(args.output)
    else:
        out = rec.with_name("events.jsonl")
    buf = io.StringIO()
    journal.write_jsonl(iter(events), buf)
    paths.write_private_atomic(out, buf.getvalue().encode("utf-8"))
    print(out)
    return 0


def cmd_status(args, cfg):
    from tmuxlog import capture
    session = _resolve_session(cfg, args.session)
    panes = _pane_dirs(session)
    info = _read_json(session / "session.json")
    print(f"session {session.name}: {session}")
    print("  boundary: %s" % info.get("boundary", "unknown"))
    if len(panes) == 0:
        print("  no panes with a hook journal yet")

    server_pid, piped = _tmux_pipes()
    for pane in panes:
        events, warnings = journal.events_from_file(pane / "hooks.rec")
        shells = len([e for e in events if e["event"] == "init"])
        ncmds = len([e for e in events if e["event"] == "cmd_start"])
        still_open = _open_commands(events)
        line = "  %s: %d shell(s), %d command(s), %d open" % (pane.name, shells, ncmds, len(still_open))
        if warnings:
            line += ", %d warning(s)" % len(warnings)
        print(line)

        meta = _read_json(pane / "meta.json")
        sealed = None
        if (pane / "sealed").exists():
            sealed = _read_json(pane / "sealed")
        if sealed:
            pipe = "sealed (%s)" % sealed.get("reason", "?")
        elif not meta or server_pid is None:
            pipe = "unknown"
        elif meta.get("server_pid") != server_pid:
            pipe = "server gone"
        elif piped.get(meta.get("pane_id")):
            pipe = "alive"
        else:
            pipe = "not piped"

        nbytes = None
        if sealed:
            nbytes = sealed.get("raw_bytes")
        if nbytes is None:
            nbytes = (pane / "raw.bin").stat().st_size if (pane / "raw.bin").exists() else 0
        gaps = capture.read_gaps(pane)
        s = f"    {meta.get('pane_id', '?')}: pipe {pipe}, raw {nbytes} bytes, {len(gaps)} gap(s)"
        if gaps:
            lost = 0
            for g in gaps:
                lost += g.get("bytes", 0)
            s += f" ({lost} bytes lost or untimed)"
        print(s)
        for ev in still_open:
            first = ev["cmd"].splitlines()[0] if ev["cmd"] else ""
            print(f"    open: seq {ev['seq']} {first}")
    return 0


def _boundary_for(cfg):
    from tmuxlog import probe
    mode = cfg["capture"].get("boundary", "auto")
    if mode == "osc" or mode == "sentinel":
        return mode
    ver = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.strip()
    cache = paths.ensure_dir(Path(cfg["paths"]["root"])) / "probe.json"
    cached = {}
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
        except json.JSONDecodeError:
            cached = {}
    if ver not in cached:
        r = probe.probe("tmux")
        cached[ver] = {
            "boundary": r["boundary"],
            "probed_ns": time.time_ns(),
            "osc_passthrough_exact": r.get("osc_passthrough_exact"),
            "osc_invisible": r.get("osc_invisible"),
        }
        paths.write_private_atomic(cache, (json.dumps(cached, indent=2) + "\n").encode())
    return cached[ver]["boundary"]


def cmd_start_session(args, cfg):
    from tmuxlog import session
    boundary = args.boundary or _boundary_for(cfg)
    res = session.start_session(args.name, cfg, boundary=boundary)
    armed = 0
    for p in res["panes"]:
        if p["status"] == "armed":
            armed += 1
    print("tmuxlog: session %s (%s): armed %d/%d pane(s) -> %s"
          % (res["session"], res["boundary"], armed, len(res["panes"]), res["dir"]))
    for p in res["panes"]:
        if p["status"] == "foreign-pipe":
            print(f"  {p['pane_id']}: skipped, already piped by something else")
    if res["created"]:
        print("  created detached session; attach with: tmux attach -t " + res["session"])
    return 0


def load_finalize(cfg):
    from tmuxlog import finalize
    c = cfg["capture"]
    finalize.ORPHAN_GAP_MS = int(c.get("orphan_gap_ms", finalize.ORPHAN_GAP_MS))
    finalize.INTERACTIVE = list(c.get("interactive", finalize.INTERACTIVE))
    return finalize


def cmd_finalize(args, cfg):
    finalize = load_finalize(cfg)
    session = _resolve_session(cfg, args.session)
    cap = cfg.get("finalize", {}).get("cap_bytes") or cfg["capture"]["max_block_bytes"]
    res = finalize.finalize(session, cap_bytes=int(cap))
    print(f"tmuxlog: finalized {session.name}: {res['blocks']} block(s) -> {res['dir']}")
    if res["gaps"]:
        print(f"  WARNING: {res['gaps']} capture gap(s); output is incomplete, see manifest.json")
    if res["warnings"]:
        print(f"  {res['warnings']} warning(s) recorded in manifest.json")
    return 0


def cmd_analyze(args, cfg):
    from tmuxlog import analyze
    load_finalize(cfg)
    a = cfg["analyze"]
    num_predict = int(a["num_predict"])
    analyze.PROMPT_RESERVE = int(a["prompt_reserve_tokens"])
    analyze.RESPONSE_RESERVE = max(int(a["response_reserve_tokens"]), num_predict, analyze.REDUCE_NUM_PREDICT)
    session = _resolve_session(cfg, args.session)
    remote = bool(a.get("allow_remote", False))

    def progress(msg):
        print("tmuxlog: " + msg, file=sys.stderr, flush=True)

    try:
        client = analyze.OllamaClient(model=args.model or a["model"],
                                      num_ctx=int(a["num_ctx"]),
                                      endpoint=a["endpoint"],
                                      retries=int(a["retries"]),
                                      timeout=float(a["timeout_s"]),
                                      allow_remote=remote,
                                      num_predict=num_predict)
        if remote:
            try:
                analyze._assert_loopback(a["endpoint"])
            except ValueError:
                print(f"tmuxlog: WARNING: sending session data to {a['endpoint']} (allow_remote=on)",
                      file=sys.stderr, flush=True)
        res = analyze.analyze(session, client, include_raw=args.include_raw,
                              collapse=bool(a["collapse_cr"]), progress=progress)
    except ValueError as e:
        raise SystemExit(f"tmuxlog: {e}")
    except analyze.OllamaError as e:
        raise SystemExit(f"tmuxlog: Ollama error: {e}")
    print(f"tmuxlog: analysis of {session.name} ({res['blocks']} block(s), {res['chunks']} chunk(s), "
          f"model {client.model}) -> {res['analysis']}")
    print(f"  prompts saved under {res['prompts_dir']}")
    return 0


def cmd_export(args, cfg):
    if args.format != "jsonl":
        raise SystemExit(f"tmuxlog: export --format {args.format} is not implemented yet")
    events, warnings = session_events(_resolve_session(cfg, args.session))
    warn_all(warnings)
    journal.write_jsonl(iter(events), sys.stdout)
    return 0


def run_command(args, cfg):
    c = args.command
    if c == "init-script":
        print(paths.INIT_SCRIPT)
        return 0
    if c == "events":
        return cmd_events(args, cfg)
    if c == "status":
        return cmd_status(args, cfg)
    if c == "capture":
        from tmuxlog import capture
        return capture.run(Path(args.pane_dir))
    if c == "start-session":
        return cmd_start_session(args, cfg)
    if c == "stop-session":
        from tmuxlog import session
        r = session.stop_session(args.name, cfg)
        print(f"tmuxlog: stopped {r['session']}: closed {r['pipes_closed']} pipe(s), "
              f"removed {r['pointers_removed']} pointer(s)")
        return 0
    if c == "arm-new":
        from tmuxlog import session
        session.arm_new(args.session, cfg)
        return 0
    if c == "finalize":
        return cmd_finalize(args, cfg)
    if c == "analyze":
        return cmd_analyze(args, cfg)
    if c == "export":
        return cmd_export(args, cfg)
    raise SystemExit("tmuxlog: unknown command " + c)


def build_parser():
    p = argparse.ArgumentParser(prog="tmuxlog", description="chronological logging for every pane of a tmux session")
    p.add_argument("--config", help="config file (default: ~/.tmuxlog/config.toml)")
    p.add_argument("--version", action="version", version="tmuxlog " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-script", help="print the path of the shell init script to source")

    s = sub.add_parser("events", help="write events.jsonl for one pane")
    s.add_argument("target", help="pane directory or hooks.rec file")
    s.add_argument("-o", "--output", help="output path, or - for stdout (default: <pane>/events.jsonl)")

    s = sub.add_parser("status", help="show panes, command counts and open commands")
    s.add_argument("--session", help="session name (default: most recently modified)")

    s = sub.add_parser("capture", help="per-pane capture process (launched by pipe-pane)")
    s.add_argument("--pane-dir", required=True)

    s = sub.add_parser("start-session", help="arm a tmux session's panes for capture")
    s.add_argument("name", nargs="?", help="tmux session name (default: current)")
    s.add_argument("--boundary", choices=["osc", "sentinel"], help="override probe")

    s = sub.add_parser("stop-session", help="tear down capture for a session")
    s.add_argument("name")

    s = sub.add_parser("arm-new", help="arm newly-created panes (called by tmux hooks)")
    s.add_argument("--session", required=True)

    s = sub.add_parser("finalize", help="write final/ master logs, events.jsonl, manifest.json")
    s.add_argument("--session", help="session name (default: most recently modified)")

    s = sub.add_parser("analyze", help="local Ollama map/reduce analysis -> analysis.md")
    s.add_argument("--session", help="session name (default: most recently modified)")
    s.add_argument("--model", help="Ollama model (default: [analyze] model)")
    s.add_argument("--include-raw", action="store_true", help="append the blocks after the analysis")

    s = sub.add_parser("export", help="export session data")
    s.add_argument("--format", required=True, choices=["raw", "jsonl", "ollama"])
    s.add_argument("--session", help="session name (default: most recently modified)")
    return p


def main(argv=None):
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    cfg = config.load(Path(args.config) if args.config else None)
    return run_command(args, cfg)
