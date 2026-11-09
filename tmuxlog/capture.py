import errno
import json
import os
import signal
import struct
import time
from pathlib import Path

from tmuxlog import paths

ARRIVAL = struct.Struct("<QQ")


class _Stop(Exception):
    pass


def write_all(fd, data):
    done = 0
    while done < len(data):
        try:
            n = os.write(fd, data[done:])
        except InterruptedError:
            continue
        except OSError:
            if done > 0:
                return done
            raise
        done += n
    return done


class Capture:

    def __init__(self, pane_dir, clock=time.time_ns):
        self.dir = Path(pane_dir)
        self.clock = clock
        paths.ensure_dir(self.dir)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        self.raw_fd = os.open(self.dir / "raw.bin", flags, paths.FILE_MODE)
        self.idx_fd = os.open(self.dir / "arrivals.idx", flags, paths.FILE_MODE)
        self.raw_offset = os.fstat(self.raw_fd).st_size
        self.stream_offset = 0
        self.gap = None
        self.pending_gaps = []
        self.gaps_total = 0
        pidfile = self.dir / "capture.pid"
        pidfile.write_text("%d %d\n" % (os.getpid(), self.clock()))
        os.chmod(pidfile, paths.FILE_MODE)

    def lost(self, kind, start, nbytes, ts, err):
        g = self.gap
        if g is not None and g["kind"] == kind and g["stream_end"] == start:
            g["stream_end"] += nbytes
            g["bytes"] += nbytes
            g["last_ts_ns"] = ts
            return
        self.end_gap()
        self.gap = {"kind": kind,
                    "stream_start": start,
                    "stream_end": start + nbytes,
                    "raw_offset": self.raw_offset,
                    "bytes": nbytes,
                    "first_ts_ns": ts,
                    "last_ts_ns": ts,
                    "error": errno.errorcode.get(err.errno, str(err.errno))}

    def end_gap(self):
        if self.gap:
            self.pending_gaps.append(self.gap)
            self.gaps_total += 1
            self.gap = None

    def flush_gaps(self):
        if len(self.pending_gaps) == 0:
            return
        lines = ""
        for g in self.pending_gaps:
            lines += json.dumps(g) + "\n"
        try:
            with paths.open_private(self.dir / "gaps.jsonl", "ab") as f:
                f.write(lines.encode())
        except OSError:
            return
        self.pending_gaps.clear()

    def feed(self, data):
        ts = self.clock()
        start = self.stream_offset
        n = len(data)
        err = OSError(errno.ENOSPC, "short write")
        try:
            written = write_all(self.raw_fd, data)
        except OSError as e:
            written = 0
            err = e

        no_ts = False
        if written:
            try:
                write_all(self.idx_fd, ARRIVAL.pack(ts, self.raw_offset))
            except OSError as e:
                no_ts = True
                self.lost("arrivals", start, written, ts, e)
            self.raw_offset += written

        if written < n:
            self.lost("raw", start + written, n - written, ts, err)
        elif not no_ts:
            self.end_gap()
        self.stream_offset = start + n
        self.flush_gaps()

    def seal(self, reason):
        self.end_gap()
        self.flush_gaps()
        for fd in [self.raw_fd, self.idx_fd]:
            try:
                os.fsync(fd)
            except OSError:
                pass
        info = {
            "ts_ns": self.clock(),
            "reason": reason,
            "stream_bytes": self.stream_offset,
            "raw_bytes": self.raw_offset,
            "gaps": self.gaps_total,
            "gaps_unwritten": self.pending_gaps,
        }
        try:
            paths.write_private_atomic(self.dir / "sealed", (json.dumps(info) + "\n").encode())
        except OSError:
            pass


def run(pane_dir, in_fd=0, clock=time.time_ns):
    def stop(signum, frame):
        raise _Stop(signal.Signals(signum).name)

    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, signal.SIG_IGN)
    cap = Capture(pane_dir, clock)
    reason = "eof"
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        while True:
            try:
                data = os.read(in_fd, 65536)
            except InterruptedError:
                continue
            if not data:
                break
            cap.feed(data)
    except _Stop as e:
        reason = "signal:" + str(e)
    finally:
        cap.seal(reason)
    return 0


def read_arrivals(pane_dir):
    data = (Path(pane_dir) / "arrivals.idx").read_bytes()
    size = ARRIVAL.size
    end = len(data) - (len(data) % size)
    return [ARRIVAL.unpack_from(data, i) for i in range(0, end, size)]


def read_gaps(pane_dir):
    f = Path(pane_dir) / "gaps.jsonl"
    if not f.exists():
        return []
    gaps = []
    for line in f.read_text().splitlines():
        try:
            gaps.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return gaps
