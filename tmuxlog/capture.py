"""Per-pane capture process, launched by `tmux pipe-pane -O` (docs/DESIGN.md §3).

Records and does not interpret: every read() of the pane's output is appended
verbatim to raw.bin, and a 16-byte arrival record (arrival_ns, raw_offset) to
arrivals.idx, both little-endian u64. Splitting into blocks happens later in
split.py, as a pure function of these files.

It never stops draining stdin: if a write fails (disk full, I/O error) the
bytes are dropped, the pane keeps running, and the hole is recorded in
gaps.jsonl as a stream byte range with timestamps. Gap records that cannot be
written yet are retried on every read and at EOF. On EOF (pane closed, pipe
closed by stop-session, server killed) or SIGTERM/SIGHUP the pane is sealed:
`sealed` gets a JSON summary, after an fsync of raw.bin and arrivals.idx.
"""

import errno
import json
import os
import signal
import struct
import time
from pathlib import Path

from tmuxlog import paths

ARRIVAL = struct.Struct("<QQ")
READ_SIZE = 65536


class _Stop(Exception):
    pass


def _write_all(fd: int, data: bytes) -> int:
    """Write as much of `data` as possible; return bytes written, raise only if none were."""
    done = 0
    while done < len(data):
        try:
            n = os.write(fd, data[done:])
        except InterruptedError:
            continue
        except OSError:
            if done:
                return done
            raise
        done += n
    return done


class Capture:
    def __init__(self, pane_dir: Path, clock=time.time_ns):
        self.dir = Path(pane_dir)
        self.clock = clock
        paths.ensure_dir(self.dir)
        self.raw_fd = self._open("raw.bin")
        self.idx_fd = self._open("arrivals.idx")
        # Re-attach appends: offsets continue from the current end of raw.bin.
        self.raw_offset = os.fstat(self.raw_fd).st_size
        self.stream_offset = 0
        self.gap = None            # open gap being extended while writes keep failing
        self.pending_gaps = []     # closed gaps not yet written to gaps.jsonl
        self.gaps_total = 0
        (self.dir / "capture.pid").write_text(f"{os.getpid()} {self.clock()}\n")
        os.chmod(self.dir / "capture.pid", paths.FILE_MODE)

    def _open(self, name: str) -> int:
        return os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, paths.FILE_MODE)

    def _lose(self, kind: str, stream_start: int, nbytes: int, ts: int, err: OSError) -> None:
        g = self.gap
        if g and g["kind"] == kind and g["stream_end"] == stream_start:
            g["stream_end"] += nbytes
            g["bytes"] += nbytes
            g["last_ts_ns"] = ts
            return
        self._close_gap()
        self.gap = {
            "kind": kind,                       # "raw": bytes lost; "arrivals": bytes kept, timestamp lost
            "stream_start": stream_start,
            "stream_end": stream_start + nbytes,
            "raw_offset": self.raw_offset,      # where the hole sits in raw.bin
            "bytes": nbytes,
            "first_ts_ns": ts,
            "last_ts_ns": ts,
            "error": errno.errorcode.get(err.errno, str(err.errno)),
        }

    def _close_gap(self) -> None:
        if self.gap:
            self.pending_gaps.append(self.gap)
            self.gaps_total += 1
            self.gap = None

    def _flush_gaps(self) -> None:
        if not self.pending_gaps:
            return
        data = "".join(json.dumps(g) + "\n" for g in self.pending_gaps).encode()
        try:
            with paths.open_private(self.dir / "gaps.jsonl", "ab") as f:
                f.write(data)
        except OSError:
            return  # disk still full; retry on the next read
        self.pending_gaps.clear()

    def feed(self, data: bytes) -> None:
        ts = self.clock()
        start, n = self.stream_offset, len(data)
        err = OSError(errno.ENOSPC, "short write")
        try:
            written = _write_all(self.raw_fd, data)
        except OSError as e:
            written, err = 0, e
        lost_ts = False
        if written:
            try:
                _write_all(self.idx_fd, ARRIVAL.pack(ts, self.raw_offset))
            except OSError as e:
                lost_ts = True
                self._lose("arrivals", start, written, ts, e)
            self.raw_offset += written
        if written < n:
            self._lose("raw", start + written, n - written, ts, err)
        elif not lost_ts:
            self._close_gap()
        self.stream_offset = start + n
        self._flush_gaps()

    def seal(self, reason: str) -> None:
        self._close_gap()
        self._flush_gaps()
        for fd in (self.raw_fd, self.idx_fd):
            try:
                os.fsync(fd)
            except OSError:
                pass
        summary = {
            "ts_ns": self.clock(),
            "reason": reason,
            "stream_bytes": self.stream_offset,
            "raw_bytes": self.raw_offset,
            "gaps": self.gaps_total,
            "gaps_unwritten": self.pending_gaps,
        }
        try:
            paths.write_private_atomic(self.dir / "sealed", (json.dumps(summary) + "\n").encode())
        except OSError:
            pass


def run(pane_dir: Path, in_fd: int = 0, clock=time.time_ns) -> int:
    def on_signal(signum, frame):
        raise _Stop(signal.Signals(signum).name)

    # Ignored until the files are open, so a signal during setup cannot kill the
    # process before it can seal; tmux itself ends a pipe by closing it (EOF).
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    cap = Capture(pane_dir, clock)
    reason = "eof"
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    try:
        while True:
            try:
                data = os.read(in_fd, READ_SIZE)
            except InterruptedError:
                continue
            if not data:
                break
            cap.feed(data)
    except _Stop as e:
        reason = f"signal:{e}"
    finally:
        cap.seal(reason)
    return 0


def read_arrivals(pane_dir: Path) -> list[tuple[int, int]]:
    """(arrival_ns, raw_offset) pairs; a torn trailing record is ignored."""
    data = (Path(pane_dir) / "arrivals.idx").read_bytes()
    usable = len(data) - len(data) % ARRIVAL.size
    return [ARRIVAL.unpack_from(data, i) for i in range(0, usable, ARRIVAL.size)]


def read_gaps(pane_dir: Path) -> list[dict]:
    p = Path(pane_dir) / "gaps.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # torn last line
    return out
