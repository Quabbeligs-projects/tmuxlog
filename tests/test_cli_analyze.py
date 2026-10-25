"""`tmuxlog analyze` CLI wiring against a mock Ollama on 127.0.0.1 (fake data only)."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tmuxlog import cli

NONCE = "b1r1"


def _session(root: Path) -> Path:
    """One pane, one command block: hooks.rec records + matching OSC marks in raw.bin."""
    pane = root / "sessions" / "demo" / "panes" / "0-1-1"
    pane.mkdir(parents=True)
    cmd, cwd = b"echo hello", b"/tmp"
    (pane / "hooks.rec").write_bytes(
        f"I {NONCE} 1700000000000000000 99 bash 5.2 1\n".encode()
        + f"S 1 1700000001000000000 {NONCE} 1 hist ".encode()
        + b"%d:%s %d:%s 4:user 4:host\n" % (len(cmd), cmd, len(cwd), cwd)
        + f"E 1 1700000002000000000 {NONCE} 0\n".encode())
    mark = lambda p: b"\x1b]6973;tmuxlog;" + p.encode() + b"\x07"
    (pane / "raw.bin").write_bytes(mark(f"I;{NONCE}") + mark(f"C;{NONCE};1") + b"hello\r\n"
                                   + mark(f"D;{NONCE};1;0"))
    (pane / "meta.json").write_text(json.dumps({"pane_id": "%0", "pane_num": 0, "session": "demo"}))
    return pane.parent.parent


class _Ollama(BaseHTTPRequestHandler):
    payloads: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).payloads.append({"path": self.path, **body})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for piece in ("## Executive Summary\nok\n", "## Findings\nnone\n"):
            self.wfile.write(json.dumps({"message": {"content": piece}, "done": False}).encode() + b"\n")
        self.wfile.write(b'{"done": true}\n')

    def log_message(self, *a):
        pass


@pytest.fixture
def mock_ollama():
    _Ollama.payloads = []
    srv = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _Ollama.payloads
    srv.shutdown()


def _config(tmp_path, endpoint, extra=""):
    p = tmp_path / "config.toml"
    p.write_text(f'[paths]\nroot = "{tmp_path / "root"}"\n'
                 f'[analyze]\nendpoint = "{endpoint}"\nnum_ctx = 16384\nretries = 1\n{extra}')
    return str(p)


def test_analyze_cli_end_to_end_with_mock(tmp_path, mock_ollama, monkeypatch, capsys):
    monkeypatch.delenv("TMUXLOG_ROOT", raising=False)
    endpoint, payloads = mock_ollama
    session = _session(tmp_path / "root")
    rc = cli.main(["--config", _config(tmp_path, endpoint), "analyze", "--session", "demo",
                   "--model", "fake-model"])
    assert rc == 0
    assert len(payloads) == 2                                  # one map chunk + reduce
    assert all(p["path"] == "/api/chat" for p in payloads)
    assert all(p["model"] == "fake-model" and p["options"]["num_ctx"] == 16384 for p in payloads)
    assert "echo hello" in payloads[0]["messages"][0]["content"]
    doc = (session / "analysis.md").read_text()
    assert doc.startswith("# tmuxlog analysis: demo") and "fake-model" in doc
    assert "## Executive Summary" in doc and "## Findings" in doc
    run_dirs = list((session / "prompts").iterdir())
    assert len(run_dirs) == 1
    assert sorted(p.name for p in run_dirs[0].iterdir()) == ["chunk-001.txt", "reduce.txt"]
    # byte-exact: the chunk keeps the pane's \r\n (read_text would normalise it)
    assert (run_dirs[0] / "chunk-001.txt").read_bytes() == payloads[0]["messages"][0]["content"].encode()
    err = capsys.readouterr().err
    assert "map chunk 1/1" in err and "reduce" in err


def test_analyze_cli_refuses_non_loopback(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUXLOG_ROOT", raising=False)
    _session(tmp_path / "root")
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", _config(tmp_path, "http://192.0.2.10:11434"), "analyze", "--session", "demo"])
    assert "non-loopback" in str(e.value)


def test_analyze_cli_ollama_down_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUXLOG_ROOT", raising=False)
    _session(tmp_path / "root")
    with socket.socket() as s:                                 # a free port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", _config(tmp_path, f"http://127.0.0.1:{port}"), "analyze", "--session", "demo"])
    assert "Ollama error" in str(e.value) and "cannot reach Ollama" in str(e.value)
