"""tmuxlog command line.

Phase 1 commands: init-script, events, status, export --format jsonl.
"""

import argparse
import io
import json
import os
import sys
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


def cmd_status(args, cfg) -> int:
    session = _resolve_session(cfg, args.session)
    panes = _pane_dirs(session)
    print(f"session {session.name}: {session}")
    if not panes:
        print("  no panes with a hook journal yet")
    for pane in panes:
        events, warnings = journal.events_from_file(pane / "hooks.rec")
        shells = sum(ev["event"] == "init" for ev in events)
        cmds = sum(ev["event"] == "cmd_start" for ev in events)
        open_cmds = _open_commands(events)
        line = f"  {pane.name}: {shells} shell(s), {cmds} command(s), {len(open_cmds)} open"
        if warnings:
            line += f", {len(warnings)} warning(s)"
        print(line)
        for ev in open_cmds:
            first = ev["cmd"].splitlines()[0] if ev["cmd"] else ""
            print(f"    open: seq {ev['seq']} {first}")
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
