"""Phase 2a: tmuxlog capture records raw bytes + arrivals, seals, and survives write failure."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tmuxlog import capture

REPO = Path(__file__).resolve().parent.parent

# Pane output as tmux would deliver it: ANSI, CR progress redraws, binary, NUL.
SAMPLE = (b"\x1b[1;32mopen\x1b[0m 22/tcp ssh\r\n"
          b"progress 10%\rprogress 99%\r\n"
          b"\x00\x01\xfe\xff binary \x1b]6973;tmuxlog;C;b1r2;1\x07after mark\r\n")


def spawn(pane_dir: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "tmuxlog", "capture", "--pane-dir", str(pane_dir)],
                            stdin=subprocess.PIPE, cwd=REPO)


def feed_chunks(proc, chunks, pause=0.05):
    for c in chunks:
        proc.stdin.write(c)
        proc.stdin.flush()
        time.sleep(pause)


def test_raw_is_verbatim_and_arrivals_cover_every_byte(tmp_path):
    chunks = [SAMPLE[:20], SAMPLE[20:57], SAMPLE[57:]]
    p = spawn(tmp_path)
    feed_chunks(p, chunks)
    p.stdin.close()
    assert p.wait(10) == 0
    assert (tmp_path / "raw.bin").read_bytes() == SAMPLE
    arr = capture.read_arrivals(tmp_path)
    offsets = [o for _, o in arr]
    # One record per read(); a late reader may merge writes, so only the
    # invariant is asserted: starts at 0, strictly increasing, inside raw.bin.
    assert offsets[0] == 0
    assert all(a < b for a, b in zip(offsets, offsets[1:]))
    assert offsets[-1] < len(SAMPLE)
    ts = [t for t, _ in arr]
    assert ts == sorted(ts)


def test_seal_on_eof_and_private_modes(tmp_path):
    p = spawn(tmp_path)
    feed_chunks(p, [SAMPLE])
    p.stdin.close()
    p.wait(10)
    sealed = json.loads((tmp_path / "sealed").read_text())
    assert sealed["reason"] == "eof"
    assert sealed["stream_bytes"] == sealed["raw_bytes"] == len(SAMPLE)
    assert sealed["gaps"] == 0
    for name in ("raw.bin", "arrivals.idx", "sealed", "capture.pid"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600, name


def test_seal_on_sigterm(tmp_path):
    p = spawn(tmp_path)
    feed_chunks(p, [SAMPLE])
    p.send_signal(signal.SIGTERM)
    p.wait(10)
    p.stdin.close()
    sealed = json.loads((tmp_path / "sealed").read_text())
    assert sealed["reason"] == "signal:SIGTERM"
    assert (tmp_path / "raw.bin").read_bytes() == SAMPLE


def test_reattach_appends_and_offsets_continue(tmp_path):
    for part in (b"first attach\r\n", b"second attach\r\n"):
        p = spawn(tmp_path)
        feed_chunks(p, [part])
        p.stdin.close()
        p.wait(10)
    assert (tmp_path / "raw.bin").read_bytes() == b"first attach\r\nsecond attach\r\n"
    offsets = [o for _, o in capture.read_arrivals(tmp_path)]
    assert offsets[0] == 0 and len(b"first attach\r\n") in offsets


def test_disk_full_keeps_draining_and_records_one_gap(tmp_path):
    (tmp_path / "raw.bin").symlink_to("/dev/full")
    p = spawn(tmp_path)
    t0 = time.time_ns()
    feed_chunks(p, [SAMPLE, SAMPLE, SAMPLE])
    p.stdin.close()
    assert p.wait(10) == 0                 # never blocked, never died
    gaps = capture.read_gaps(tmp_path)
    assert len(gaps) == 1, gaps            # consecutive failures merge into one range
    g = gaps[0]
    assert g["kind"] == "raw" and g["error"] == "ENOSPC"
    assert (g["stream_start"], g["stream_end"], g["bytes"]) == (0, 3 * len(SAMPLE), 3 * len(SAMPLE))
    assert t0 <= g["first_ts_ns"] <= g["last_ts_ns"]
    sealed = json.loads((tmp_path / "sealed").read_text())
    assert sealed["stream_bytes"] == 3 * len(SAMPLE) and sealed["raw_bytes"] == 0 and sealed["gaps"] == 1


def test_gap_in_the_middle_is_a_precise_stream_range(tmp_path):
    clock = iter(range(1000, 2000)).__next__
    cap = capture.Capture(tmp_path, clock=clock)
    real_fd = cap.raw_fd
    cap.feed(b"AAAA")
    cap.raw_fd = os.open("/dev/full", os.O_WRONLY)
    cap.feed(b"BB")
    cap.feed(b"CCC")
    os.close(cap.raw_fd)
    cap.raw_fd = real_fd
    cap.feed(b"DD")
    cap.seal("eof")
    assert (tmp_path / "raw.bin").read_bytes() == b"AAAADD"
    (g,) = capture.read_gaps(tmp_path)
    assert (g["stream_start"], g["stream_end"], g["raw_offset"]) == (4, 9, 4)
    assert g["first_ts_ns"] < g["last_ts_ns"]
    assert [o for _, o in capture.read_arrivals(tmp_path)] == [0, 4]


def test_torn_arrival_record_is_ignored(tmp_path):
    (tmp_path / "arrivals.idx").write_bytes(capture.ARRIVAL.pack(5, 0) + b"\x01\x02\x03")
    assert capture.read_arrivals(tmp_path) == [(5, 0)]
