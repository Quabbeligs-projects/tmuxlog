import argparse
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tmuxlog import __version__, config, journal, paths

def _pane_dirs(session: Path) -> list[Path]:
    panes = session / "panes"
    if not panes.is_dir():
        return []
    return sorted(p for p in panes.iterdir() if (p / "hooks.rec").is_file())

def _resolve_session(cfg: dict, name: str | None) -> Path:
    root = Path(cfg["paths"]["root"])
    if name:
        return paths.session_dir(root, name)
    sessions = root / "sessions"
    candidates = sorted(sessions.iterdir(), key=lambda p: p.stat().st_mtime) if sessions.is_dir() else []
    if not candidates:
        raise SystemExit(f"tmuxlog: no sessions under {sessions}")
    return candidates[-1]

def _resolve_hooks_rec(target: Path) -> Path:
    return target / "hooks.rec" if target.is_dir() else target

def _open_commands(events: list[dict]) -> list[dict]:
    started = {}
    for ev in events:
        key = (ev.get("nonce"), ev.get("seq"))
        if ev["event"] == "cmd_start":
            started[key] = ev
        elif ev["event"] == "cmd_end":
            started.pop(key, None)
    return list(started.values())

def session_events(session: Path) -> tuple[list[dict], list[dict]]:
    """All panes' events, each tagged with its pane, ordered by ts_ns then pane."""
    events, warnings = [], []
    for pane in _pane_dirs(session):
        evs, warns = journal.events_from_file(pane / "hooks.rec")
        events += [{**ev, "pane": pane.name} for ev in evs]
        warnings += [{**w, "pane": pane.name} for w in warns]
    events.sort(key=lambda ev: (ev["ts_ns"], ev["pane"]))
    return events, warnings

def cmd_init_script(args, cfg) -> int:
    print(paths.INIT_SCRIPT)
    return 0

def cmd_events(args, cfg) -> int:
    """Write a pane's events.jsonl next to its hooks.rec (or to stdout with -o -)."""
    rec = _resolve_hooks_rec(Path(args.target))
    events, warnings = journal.events_from_file(rec)
    for w in warnings:
        print("tmuxlog: warning:", json.dumps(w), file=sys.stderr)
    if args.output == "-":
        journal.write_jsonl(iter(events), sys.stdout)
        return 0
    out = Path(args.output) if args.output else rec.with_name("events.jsonl")
    buf = io.StringIO()
    journal.write_jsonl(iter(events), buf)
    paths.write_private_atomic(out, buf.getvalue().encode("utf-8"))
    print(out)
    return 0

def _tmux_pipes() -> tuple[int | None, dict[str, bool]]:
    """(server pid, {pane_id: piped}) of the default tmux server, or (None, {})."""
    try:
        pid = subprocess.run(["tmux", "display-message", "-p", "#{pid}"],
                             capture_output=True, text=True, timeout=5)
        panes = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pipe}"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None, {}
    if pid.returncode or panes.returncode:
        return None, {}
    piped = {}
    for line in panes.stdout.splitlines():
        pane_id, _, flag = line.partition(" ")
        piped[pane_id] = flag == "1"
    return int(pid.stdout.strip()), piped

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

def cmd_status(args, cfg) -> int:
    from tmuxlog import capture
    session = _resolve_session(cfg, args.session)
    panes = _pane_dirs(session)
    sj = _read_json(session / "session.json")
    print(f"session {session.name}: {session}")
    print(f"  boundary: {sj.get('boundary', 'unknown')}")
    if not panes:
        print("  no panes with a hook journal yet")
    server_pid, piped = _tmux_pipes()
    for pane in panes:
        events, warnings = journal.events_from_file(pane / "hooks.rec")
        shells = sum(ev["event"] == "init" for ev in events)
        cmds = sum(ev["event"] == "cmd_start" for ev in events)
        open_cmds = _open_commands(events)
        line = f"  {pane.name}: {shells} shell(s), {cmds} command(s), {len(open_cmds)} open"
        if warnings:
            line += f", {len(warnings)} warning(s)"
        print(line)

        meta = _read_json(pane / "meta.json")
        sealed = _read_json(pane / "sealed") if (pane / "sealed").exists() else None
        if sealed:
            pipe = f"sealed ({sealed.get('reason', '?')})"
        elif not meta or server_pid is None:
            pipe = "unknown"
        elif meta.get("server_pid") != server_pid:
            pipe = "server gone"
        else:
            pipe = "alive" if piped.get(meta.get("pane_id")) else "not piped"
        raw_bytes = (sealed or {}).get("raw_bytes")
        if raw_bytes is None:
            raw_bytes = (pane / "raw.bin").stat().st_size if (pane / "raw.bin").exists() else 0
        gaps = capture.read_gaps(pane)
        detail = f"    {meta.get('pane_id', '?')}: pipe {pipe}, raw {raw_bytes} bytes, {len(gaps)} gap(s)"
        if gaps:
            detail += f" ({sum(g.get('bytes', 0) for g in gaps)} bytes lost or untimed)"
        print(detail)
        for ev in open_cmds:
            first = ev["cmd"].splitlines()[0] if ev["cmd"] else ""
            print(f"    open: seq {ev['seq']} {first}")
    return 0

def cmd_capture(args, cfg) -> int:
    from tmuxlog import capture
    return capture.run(Path(args.pane_dir))

def _boundary_for(cfg) -> str:
    """config `capture.boundary`, or for "auto" the probe result cached per tmux version."""
    from tmuxlog import probe
    mode = cfg["capture"].get("boundary", "auto")
    if mode in ("osc", "sentinel"):
        return mode
    version = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.strip()
    cache = paths.ensure_dir(Path(cfg["paths"]["root"])) / "probe.json"
    cached = {}
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
        except json.JSONDecodeError:
            cached = {}
    if version not in cached:
        result = probe.probe("tmux")
        cached[version] = {"boundary": result["boundary"], "probed_ns": time.time_ns(),
                           "osc_passthrough_exact": result.get("osc_passthrough_exact"),
                           "osc_invisible": result.get("osc_invisible")}
        paths.write_private_atomic(cache, (json.dumps(cached, indent=2) + "\n").encode())
    return cached[version]["boundary"]

def cmd_start_session(args, cfg) -> int:
    from tmuxlog import session
    res = session.start_session(args.name, cfg, boundary=args.boundary or _boundary_for(cfg))
    armed = sum(p["status"] == "armed" for p in res["panes"])
    print(f"tmuxlog: session {res['session']} ({res['boundary']}): armed {armed}/"
          f"{len(res['panes'])} pane(s) -> {res['dir']}")
    for p in res["panes"]:
        if p["status"] == "foreign-pipe":
            print(f"  {p['pane_id']}: skipped, already piped by something else")
    if res["created"]:
        print(f"  created detached session; attach with: tmux attach -t {res['session']}")
    return 0

def cmd_arm_new(args, cfg) -> int:
    from tmuxlog import session
    session.arm_new(args.session, cfg)
    return 0

def cmd_stop_session(args, cfg) -> int:
    from tmuxlog import session
    res = session.stop_session(args.name, cfg)
    print(f"tmuxlog: stopped {res['session']}: closed {res['pipes_closed']} pipe(s), "
          f"removed {res['pointers_removed']} pointer(s)")
    return 0

def cmd_finalize(args, cfg) -> int:
    from tmuxlog import finalize
    session = _resolve_session(cfg, args.session)
    cap = cfg.get("finalize", {}).get("cap_bytes") or cfg["capture"]["max_block_bytes"]
    res = finalize.finalize(session, cap_bytes=int(cap))
    print(f"tmuxlog: finalized {session.name}: {res['blocks']} block(s) -> {res['dir']}")
    if res["gaps"]:
        print(f"  WARNING: {res['gaps']} capture gap(s); output is incomplete, see manifest.json")
    if res["warnings"]:
        print(f"  {res['warnings']} warning(s) recorded in manifest.json")
    return 0

def cmd_analyze(args, cfg) -> int:
    from tmuxlog import analyze
    a = cfg["analyze"]
    num_predict = int(a["num_predict"])
    analyze.PROMPT_RESERVE = int(a["prompt_reserve_tokens"])
    analyze.RESPONSE_RESERVE = max(int(a["response_reserve_tokens"]), num_predict,
                                   analyze.REDUCE_NUM_PREDICT)
    session = _resolve_session(cfg, args.session)
    allow_remote = bool(a.get("allow_remote", False))
    try:
        client = analyze.OllamaClient(model=args.model or a["model"], num_ctx=int(a["num_ctx"]),
                                      endpoint=a["endpoint"], retries=int(a["retries"]),
                                      timeout=float(a["timeout_s"]), allow_remote=allow_remote,
                                      num_predict=num_predict)
        if allow_remote:
            try:
                analyze._assert_loopback(a["endpoint"])
            except ValueError:
                print(f"tmuxlog: WARNING: sending session data to {a['endpoint']} (allow_remote=on)",
                      file=sys.stderr, flush=True)
        res = analyze.analyze(session, client, include_raw=args.include_raw,
                              collapse=bool(a["collapse_cr"]),
                              progress=lambda msg: print(f"tmuxlog: {msg}", file=sys.stderr, flush=True))
    except ValueError as e:
        raise SystemExit(f"tmuxlog: {e}")
    except analyze.OllamaError as e:
        raise SystemExit(f"tmuxlog: Ollama error: {e}")
    print(f"tmuxlog: analysis of {session.name} ({res['blocks']} block(s), {res['chunks']} chunk(s), "
          f"model {client.model}) -> {res['analysis']}")
    print(f"  prompts saved under {res['prompts_dir']}")
    return 0

def cmd_export(args, cfg) -> int:
    if args.format != "jsonl":
        raise SystemExit(f"tmuxlog: export --format {args.format} is not implemented yet")
    events, warnings = session_events(_resolve_session(cfg, args.session))
    for w in warnings:
        print("tmuxlog: warning:", json.dumps(w), file=sys.stderr)
    journal.write_jsonl(iter(events), sys.stdout)
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tmuxlog", description=__doc__.splitlines()[0])
    p.add_argument("--config", help="config file (default: ~/.tmuxlog/config.toml)")
    p.add_argument("--version", action="version", version=f"tmuxlog {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-script", help="print the path of the shell init script to source")
    s.set_defaults(func=cmd_init_script)

    s = sub.add_parser("events", help="write events.jsonl for one pane")
    s.add_argument("target", help="pane directory or hooks.rec file")
    s.add_argument("-o", "--output", help="output path, or - for stdout (default: <pane>/events.jsonl)")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("status", help="show panes, command counts and open commands")
    s.add_argument("--session", help="session name (default: most recently modified)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("capture", help="per-pane capture process (launched by pipe-pane)")
    s.add_argument("--pane-dir", required=True)
    s.set_defaults(func=cmd_capture)

    s = sub.add_parser("start-session", help="arm a tmux session's panes for capture")
    s.add_argument("name", nargs="?", help="tmux session name (default: current)")
    s.add_argument("--boundary", choices=["osc", "sentinel"], help="override probe")
    s.set_defaults(func=cmd_start_session)

    s = sub.add_parser("stop-session", help="tear down capture for a session")
    s.add_argument("name")
    s.set_defaults(func=cmd_stop_session)

    s = sub.add_parser("arm-new", help="arm newly-created panes (called by tmux hooks)")
    s.add_argument("--session", required=True)
    s.set_defaults(func=cmd_arm_new)

    s = sub.add_parser("finalize", help="write final/ master logs, events.jsonl, manifest.json")
    s.add_argument("--session", help="session name (default: most recently modified)")
    s.set_defaults(func=cmd_finalize)

    s = sub.add_parser("analyze", help="local Ollama map/reduce analysis -> analysis.md")
    s.add_argument("--session", help="session name (default: most recently modified)")
    s.add_argument("--model", help="Ollama model (default: [analyze] model)")
    s.add_argument("--include-raw", action="store_true", help="append the blocks after the analysis")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("export", help="export session data")
    s.add_argument("--format", required=True, choices=["raw", "jsonl", "ollama"])
    s.add_argument("--session", help="session name (default: most recently modified)")
    s.set_defaults(func=cmd_export)
    return p

def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    cfg = config.load(Path(args.config) if args.config else None)
    return args.func(args, cfg)
