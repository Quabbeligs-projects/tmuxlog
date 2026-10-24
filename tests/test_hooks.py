"""Integration tests for the bash and zsh hooks, driven through a real pty.

These run interactive bash/zsh with the tmuxlog init sourced (tests/ptyshell.py),
exercise real command lines, and parse the resulting per-pane hooks.rec with the
journal parser. They assert the behaviours the design hinges on — most of all
that a space-prefixed pipeline under ignorespace is captured as the command that
was actually run (not the previous one) with every stage preserved.

Runs under pytest, or standalone: ``python3 tests/test_hooks.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/  -> ptyshell
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo    -> tmuxlog

from ptyshell import Shell          # noqa: E402

try:
    from tmuxlog import journal      # noqa: E402
except ImportError:
    import journal                   # noqa: E402


class SkipTest(Exception):
    pass


def _skip(msg):
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        raise SkipTest(msg)


HAVE_ZSH = shutil.which("zsh") is not None


def _comma_locale() -> str | None:
    try:
        avail = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()
    except Exception:
        return None
    for want in ("de_DE.UTF-8", "de_DE.utf8", "de_AT.UTF-8", "fr_FR.UTF-8", "fr_FR.utf8"):
        if want in avail:
            return want
    return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _events(pane: Path):
    f = pane / "hooks.rec"
    if not f.exists():
        return [], []
    recs, warns = journal.parse(f.read_bytes())
    return list(journal.to_events(recs)), warns


def _raw_records(pane: Path):
    f = pane / "hooks.rec"
    return journal.parse(f.read_bytes())[0] if f.exists() else []


def _setup(shell: str, rc_pre: str = "", env_extra: dict | None = None, make_pane: bool = True):
    tmp = Path(tempfile.mkdtemp(prefix="tlhook_"))
    home = tmp / "home"; home.mkdir()
    pane = tmp / "pane"
    if make_pane:
        pane.mkdir()
    env = {"TMUXLOG_PANE_DIR": str(pane), "TMUXLOG_ROOT": str(tmp / "root")}
    if env_extra:
        env.update(env_extra)
    sh = Shell(shell, home, env=env, rc_pre=rc_pre)
    return sh, pane, tmp


def _run(shell: str, lines, rc_pre: str = "", env_extra: dict | None = None):
    """Run lines, read events BEFORE closing (so the trailing `exit` is excluded)."""
    sh, pane, tmp = _setup(shell, rc_pre, env_extra)
    try:
        for ln in lines:
            sh.run(ln)
        sh.pump(0.2)
        events, warns = _events(pane)
        out = bytes(sh.out)
    finally:
        sh.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return events, warns, out


def _starts(events):
    return [e for e in events if e["event"] == "cmd_start"]


def _pairs(events):
    """(cmd, exit) per completed command, matched by (nonce, seq)."""
    started, out = {}, []
    for e in events:
        key = (e.get("nonce"), e.get("seq"))
        if e["event"] == "cmd_start":
            started[key] = e["cmd"]
        elif e["event"] == "cmd_end":
            out.append((started.get(key), e["exit"]))
    return out


def _full(events, seq):
    """Reconstruct a command line: S text + its T parts in order."""
    base, parts = None, []
    for e in events:
        if e.get("seq") != seq:
            continue
        if e["event"] == "cmd_start":
            base = e["cmd"]
        elif e["event"] == "cmd_part":
            parts.append((e["part"], e["cmd"]))
    parts.sort()
    return base, [c for _, c in parts]


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

def test_bash_init_record_fields():
    events, _, _ = _run("bash", ["echo hi"])
    assert events and events[0]["event"] == "init"
    i = events[0]
    assert i["shell"] == "bash"
    assert i["shell_version"] and "bash" not in i["shell_version"].lower()
    assert isinstance(i["shlvl"], int) and i["shlvl"] >= 1


def test_bash_simple_command_no_spurious_starts():
    events, warns, _ = _run("bash", ["echo hello"])
    assert warns == []
    cmds = [s["cmd"] for s in _starts(events)]
    # exactly the one command we ran — prompt machinery/completion created none
    assert cmds == ["echo hello"], cmds
    assert ("echo hello", 0) in _pairs(events)


def test_bash_exit_codes():
    events, _, _ = _run("bash", ["true", "false"])
    pairs = _pairs(events)
    assert ("true", 0) in pairs and ("false", 1) in pairs


def test_bash_dollar_question_preserved():
    # if the prompt hooks clobbered $?, this would read RC=0
    _, _, out = _run("bash", ["false", "echo RC=$?"])
    assert b"RC=1" in out, out


def test_bash_pipeline_history_on_full_line():
    events, _, _ = _run("bash", ["echo AAA | grep AAA"])
    s = _starts(events)[0]
    base, parts = _full(events, s["seq"])
    joined = " ".join([base] + parts)
    assert "echo AAA" in joined and "grep AAA" in joined


def test_bash_pipeline_ignorespace_all_stages_and_right_command():
    # THE critical case: space-prefixed line under ignorespace is not saved to
    # history, so it must come from BASH_COMMAND (+ T parts), and must be the
    # command just run — never the previous one.
    events, _, _ = _run(
        "bash",
        ["echo DECOY", "  echo NEEDLE | cat"],
        rc_pre="HISTCONTROL=ignorespace",
    )
    starts = _starts(events)
    assert len(starts) == 2, [s["cmd"] for s in starts]
    tgt = starts[1]
    assert tgt["cmd"] == "echo NEEDLE", tgt["cmd"]      # not "echo DECOY"
    assert tgt.get("src") == "bc"
    base, parts = _full(events, tgt["seq"])
    assert base == "echo NEEDLE" and parts == ["cat"], (base, parts)


def test_bash_heredoc_full_body():
    events, _, _ = _run("bash", ["cat <<EOF\nalpha: beta\nEOF"])
    s = _starts(events)[0]
    assert "cat <<EOF" in s["cmd"] and "alpha: beta" in s["cmd"], s["cmd"]


def test_bash_ctrl_c_exit_130():
    sh, pane, tmp = _setup("bash")
    try:
        n = sh.prompts()
        sh.send("sleep 3\r")
        sh.pump(0.5)                       # let sleep start
        sh.send("\x03")                    # SIGINT
        sh.wait_for(lambda: sh.prompts() > n, timeout=10)
        sh.pump(0.2)
        events, _ = _events(pane)
    finally:
        sh.close()
        shutil.rmtree(tmp, ignore_errors=True)
    pairs = dict((c, e) for c, e in _pairs(events))
    assert "sleep 3" in pairs, pairs
    assert pairs["sleep 3"] == 130, pairs


def test_bash_timestamps_are_digit_strings_and_ordered():
    _assert_ts_ok("bash", env_extra=None)


def test_bash_timestamps_under_comma_locale():
    loc = _comma_locale()
    if not loc:
        _skip("no comma-radix locale installed")
    # note A: EPOCHREALTIME prints a comma here; the hook must still log digits
    _assert_ts_ok("bash", env_extra={"LC_ALL": loc})


def _assert_ts_ok(shell, env_extra):
    sh, pane, tmp = _setup(shell, env_extra=env_extra)
    try:
        sh.run("echo one")
        sh.run("echo two")
        sh.pump(0.2)
        recs = _raw_records(pane)
    finally:
        sh.close()
        shutil.rmtree(tmp, ignore_errors=True)
    ts = [r["ts_ns"] for r in recs if "ts_ns" in r]
    assert ts, "no timestamps recorded"
    for t in ts:
        assert t.isdigit(), f"ts not all-digits: {t!r}"
    ints = [int(t) for t in ts]
    assert ints == sorted(ints), "timestamps not monotonic"
    assert all(len(t) >= 19 for t in ts), "timestamps not nanosecond-scale"


def test_bash_rearm_when_pointer_appears():
    # pane dir does not exist at startup -> hooks load but never arm; once the
    # dir appears, the next prompt arms and later commands are logged.
    sh, pane, tmp = _setup("bash", make_pane=False)
    try:
        sh.run("echo before")              # not armed: nothing written
        assert not (pane / "hooks.rec").exists()
        pane.mkdir()
        sh.run("echo trigger")             # this prompt arms the hooks
        sh.run("echo after")               # armed: logged
        sh.pump(0.2)
        events, _ = _events(pane)
    finally:
        sh.close()
        shutil.rmtree(tmp, ignore_errors=True)
    cmds = [s["cmd"] for s in _starts(events)]
    assert any(e["event"] == "init" for e in events)
    assert "echo after" in cmds
    assert "echo before" not in cmds


# ---------------------------------------------------------------------------
# zsh
# ---------------------------------------------------------------------------

def test_zsh_init_and_simple():
    if not HAVE_ZSH:
        _skip("zsh not installed")
    events, warns, _ = _run("zsh", ["echo hi"])
    assert warns == []
    assert events[0]["event"] == "init" and events[0]["shell"] == "zsh"
    assert events[0]["shell_version"] and isinstance(events[0]["shlvl"], int)
    s = _starts(events)[0]
    assert s["cmd"] == "echo hi" and s.get("src") == "px"


def test_zsh_pipeline_single_S_no_T():
    if not HAVE_ZSH:
        _skip("zsh not installed")
    events, _, _ = _run("zsh", ["echo A | grep A"])
    s = _starts(events)[0]
    assert "echo A" in s["cmd"] and "grep A" in s["cmd"]
    assert not any(e["event"] == "cmd_part" for e in events), "zsh must not emit T"


def test_zsh_exit_code():
    if not HAVE_ZSH:
        _skip("zsh not installed")
    events, _, _ = _run("zsh", ["false"])
    assert ("false", 1) in _pairs(events)


def test_zsh_timestamps_are_digit_strings():
    if not HAVE_ZSH:
        _skip("zsh not installed")
    _assert_ts_ok("zsh", env_extra=None)


# ---------------------------------------------------------------------------
# standalone runner (so it works without pytest installed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback
    names = sorted(k for k, v in list(globals().items())
                   if k.startswith("test_") and callable(v))
    passed = failed = skipped = 0
    for name in names:
        try:
            globals()[name]()
            passed += 1
            print(f"PASS {name}")
        except SkipTest as e:
            skipped += 1
            print(f"SKIP {name}: {e}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped, {len(names)} total")
    raise SystemExit(1 if failed else 0)
