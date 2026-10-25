"""Tests for analyze.py: chunking, the Ollama client seam, and orchestration.

No real Ollama (not reachable in CI/containers). The client's HTTP sits behind
a transport seam, so chunking, num_ctx, retry/backoff, prompt logging, loopback
validation, and analysis.md structure are all tested without a socket. One test
uses a tiny in-process mock HTTP server to exercise the default urllib transport
end to end (num_ctx on the wire + streaming parse).

NOTE: the real-model run (actual mistral via Ollama) is a local check for the
user; these tests prove the plumbing around it.

Standalone: ``python3 tests/test_analyze.py``.
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmuxlog import analyze                 # noqa: E402

ARRIVAL = struct.Struct("<QQ")


# --- session builders (reuse split/finalize shapes) ------------------------

def mark(kind, nonce, seq=None, exit=None):
    body = f"6973;tmuxlog;{kind};{nonce}"
    if seq is not None:
        body += f";{seq}"
    if exit is not None:
        body += f";{exit}"
    return ("\x1b]" + body + "\x07").encode()


def _b(x):
    return x if isinstance(x, (bytes, bytearray)) else str(x).encode()


def _lf(v, last):
    v = _b(v)
    return str(len(v)).encode() + b":" + v + (b"\n" if last else b" ")


def rec_I(nonce, ts):
    return b"I %s %s 1 bash 5.2 1\n" % (_b(nonce), _b(ts))


def rec_S(seq, ts, nonce, cmd):
    return (b"S %s %s %s 1 bc " % (_b(seq), _b(ts), _b(nonce))
            + _lf(cmd, False) + _lf("/root", False) + _lf("root", False) + _lf("kali", True))


def rec_E(seq, ts, nonce, ec):
    return b"E %s %s %s %s\n" % (_b(seq), _b(ts), _b(nonce), _b(ec))


def cycle(nonce, seq, echo, out, exit=0):
    return (mark("A", nonce) + b"$ " + mark("B", nonce) + echo + b"\r\n"
            + mark("C", nonce, seq) + out + mark("D", nonce, seq, exit))


def make_session(n_cmds=3, out=b"output\r\n"):
    tmp = Path(tempfile.mkdtemp(prefix="tlan_"))
    sd = tmp / "sessions" / "s"
    pane = sd / "panes" / "0-1-1"
    pane.mkdir(parents=True)
    raw = mark("I", "a")
    hooks = rec_I("a", 10)
    for i in range(1, n_cmds + 1):
        raw += cycle("a", i, f"cmd{i}".encode(), out)
        hooks += rec_S(i, 100 * i, "a", f"cmd{i}") + rec_E(i, 100 * i + 5, "a", 0)
    (pane / "raw.bin").write_bytes(raw)
    (pane / "hooks.rec").write_bytes(hooks)
    (pane / "sealed").write_text('{"reason":"eof"}\n')
    (pane / "meta.json").write_text(json.dumps(
        {"pane_uid": "0-1-1", "pane_id": "%0", "pane_num": "0", "session": "s",
         "window_index": "0", "pane_index": "0", "created_ns": 1}))
    return tmp, sd


# --- fake client -----------------------------------------------------------

class FakeClient:
    """Captures prompts and num_ctx; returns canned replies. No network."""

    def __init__(self, num_ctx=32768, model="fake", reply="SUMMARY"):
        self.num_ctx = num_ctx
        self.num_predict = 1024
        self.model = model
        self.reply = reply
        self.prompts = []

    def chat(self, prompt, on_token=None, num_predict=None):
        self.prompts.append(prompt)
        self.last_num_predict = num_predict
        return f"{self.reply}#{len(self.prompts)}"


# --- chunking --------------------------------------------------------------

def test_whole_blocks_never_split_when_they_fit():
    items = [("block 1", "a" * 30), ("block 2", "b" * 30), ("block 3", "c" * 30)]
    # budget comfortably larger than one block, smaller than all three
    chunks = analyze.chunk_blocks(items, budget=25)   # 30 bytes ~ 10 tokens each
    # each block is ~10 tokens; 25-token budget packs 2 per chunk
    flat = [p["label"] for ch in chunks for p in ch]
    assert flat == ["block 1", "block 2", "block 3"]        # order preserved
    for ch in chunks:
        assert sum(analyze.est_tokens(p["text"]) for p in ch) <= 25
    # no label appears split (no "part")
    assert not any("part" in p["label"] for ch in chunks for p in ch)


def test_oversized_block_split_with_part_labels():
    big = "x" * 3000                       # ~1000 tokens
    items = [("block 1", "small"), ("block 2", big)]
    chunks = analyze.chunk_blocks(items, budget=200)    # 200 tokens = 600 bytes/part
    labels = [p["label"] for ch in chunks for p in ch]
    assert "block 1" in labels
    parts = [l for l in labels if l.startswith("block 2 part")]
    assert len(parts) >= 2 and parts[0] == "block 2 part 1/%d" % len(parts)
    # reassembling the parts recovers the whole block text
    joined = "".join(p["text"] for ch in chunks for p in ch if p["label"].startswith("block 2 part"))
    assert joined == big


def test_budget_rejects_tiny_num_ctx():
    try:
        analyze.budget_tokens(100)
        assert False
    except ValueError:
        pass


# --- client seam -----------------------------------------------------------

def test_payload_always_sets_num_ctx():
    c = analyze.OllamaClient(model="m", num_ctx=12345, transport=lambda *a: iter(()))
    p = c.build_payload("hi")
    assert p["options"]["num_ctx"] == 12345
    assert p["model"] == "m" and p["stream"] is True


def test_payload_sets_num_predict():
    c = analyze.OllamaClient(num_predict=777, transport=lambda *a: iter(()))
    assert c.build_payload("hi")["options"]["num_predict"] == 777
    # per-call override wins (used for the longer reduce pass)
    assert c.build_payload("hi", num_predict=50)["options"]["num_predict"] == 50


def test_reduce_pass_hierarchical():
    # summaries too big to fit one reduce -> grouped, reduced, then reduced again
    class C:
        num_ctx = 4096          # budget = 4096 - 1024 - 2048 = 1024 tokens
        model = "x"

        def __init__(self):
            self.calls = []

        def chat(self, prompt, on_token=None, num_predict=None):
            self.calls.append(num_predict)
            return "R"           # tiny, so the next level collapses to one group

    import tempfile as _t
    pdir = Path(_t.mkdtemp())
    try:
        client = C()
        summaries = ["S" * 1200 for _ in range(5)]   # ~400 tokens each -> 3 groups
        out = analyze.reduce_pass(client, summaries, pdir)
        assert out == "R"
        assert len(client.calls) > 1                  # it recursed, didn't do one call
        assert all(n == analyze.REDUCE_NUM_PREDICT for n in client.calls)
        assert (pdir / "reduce.txt").exists()
        assert list(pdir.glob("reduce-L1-*.txt"))     # intermediate level saved
    finally:
        shutil.rmtree(pdir, ignore_errors=True)


def test_loopback_enforced():
    for bad in ("http://10.0.0.5:11434", "http://example.com:11434"):
        try:
            analyze.OllamaClient(endpoint=bad)
            assert False, bad
        except ValueError:
            pass
    for ok in ("http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"):
        analyze.OllamaClient(endpoint=ok)   # must not raise


def test_remote_endpoint_requires_opt_in():
    remote = "http://192.168.1.50:11434"
    # refused by default
    try:
        analyze.OllamaClient(endpoint=remote)
        assert False, "non-loopback must be refused without allow_remote"
    except ValueError:
        pass
    # permitted with explicit opt-in
    c = analyze.OllamaClient(endpoint=remote, allow_remote=True)
    assert c.endpoint == remote


def test_streaming_content_accumulated():
    def transport(url, body, timeout):
        for piece in ("hel", "lo ", "world"):
            yield (json.dumps({"message": {"content": piece}, "done": False}) + "\n").encode()
        yield (json.dumps({"message": {"content": ""}, "done": True}) + "\n").encode()
    c = analyze.OllamaClient(transport=transport)
    tokens = []
    assert c.chat("p", on_token=tokens.append) == "hello world"
    assert tokens == ["hel", "lo ", "world"]


def test_retry_then_success():
    calls = {"n": 0}

    def transport(url, body, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("refused")
        yield (json.dumps({"message": {"content": "ok"}}) + "\n").encode()
    slept = []
    c = analyze.OllamaClient(transport=transport, retries=3, backoff=0.01,
                             sleep=slept.append)
    assert c.chat("p") == "ok"
    assert calls["n"] == 3 and len(slept) == 2         # backed off twice


def test_connection_failure_surfaces_after_retries():
    def transport(url, body, timeout):
        raise ConnectionError("no ollama")
        yield  # pragma: no cover
    c = analyze.OllamaClient(transport=transport, retries=2, backoff=0.0,
                             sleep=lambda s: None)
    try:
        c.chat("p")
        assert False
    except analyze.OllamaError as e:
        assert "cannot reach Ollama" in str(e)


def test_ollama_error_body_surfaced():
    def transport(url, body, timeout):
        yield (json.dumps({"error": "model 'mistral' not found"}) + "\n").encode()
    c = analyze.OllamaClient(transport=transport)
    try:
        c.chat("p")
        assert False
    except analyze.OllamaError as e:
        assert "not found" in str(e)


# --- default transport over a real loopback socket -------------------------

def test_default_transport_sends_num_ctx_and_streams():
    received = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers["Content-Length"])
            received["payload"] = json.loads(self.rfile.read(n))
            self.send_response(200)
            self.end_headers()
            for piece in ("127.", "0.0.1"):
                self.wfile.write((json.dumps({"message": {"content": piece}}) + "\n").encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    port = srv.server_address[1]
    try:
        c = analyze.OllamaClient(num_ctx=4096, endpoint=f"http://127.0.0.1:{port}")
        assert c.chat("hello") == "127.0.0.1"
        assert received["payload"]["options"]["num_ctx"] == 4096     # on the wire
    finally:
        srv.server_close()


# --- orchestration ---------------------------------------------------------

def test_analyze_writes_doc_and_prompts():
    tmp, sd = make_session(n_cmds=3)
    try:
        c = FakeClient()
        res = analyze.analyze(sd, c, run_ts="RUN1")
        doc = (sd / "analysis.md").read_text()
        assert doc.startswith("# tmuxlog analysis: s")
        assert "model: fake (num_ctx=32768)" in doc
        assert "blocks: 3" in doc
        # the reduce output is the last FakeClient reply, embedded in the doc
        assert c.prompts, "no prompts sent"
        assert f"SUMMARY#{len(c.prompts)}" in doc
        # map prompts + reduce prompt saved verbatim
        pdir = sd / "prompts" / "RUN1"
        assert (pdir / "reduce.txt").exists()
        assert list(pdir.glob("chunk-*.txt"))
        # reduce prompt asks for the required sections
        reduce = (pdir / "reduce.txt").read_text()
        for sec in ("Executive Summary", "Chronological Narrative",
                    "Findings", "Open Threads"):
            assert sec in reduce
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_analyze_include_raw_appends_raw_with_escapes():
    # --include-raw must append the RAW block (escapes preserved), not the clean one
    tmp, sd = make_session(n_cmds=1, out=b"\x1b[31mUNIQUEMARKER\x1b[0m\r\n")
    try:
        analyze.analyze(sd, FakeClient(), include_raw=True, run_ts="RUN2")
        doc = (sd / "analysis.md").read_text()
        assert "## Raw blocks" in doc and "UNIQUEMARKER" in doc
        assert "\x1b[31m" in doc           # raw escapes present, i.e. not stripped
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_analyze_header_has_byte_counts():
    tmp, sd = make_session(n_cmds=2)
    try:
        analyze.analyze(sd, FakeClient(), run_ts="RUNH")
        doc = (sd / "analysis.md").read_text()
        assert "bytes: raw=" in doc and "clean=" in doc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_collapse_cr_progress_bar():
    # a gobuster-style progress line collapses to its final state
    assert analyze.collapse_cr(" 10%\r 55%\r100%") == "100%"
    assert analyze.collapse_cr("abc\x08\x08XY") == "aXY"
    # newlines preserved; only within-line CR collapses
    assert analyze.collapse_cr("line1\r\nkeep\r\n") == "line1\nkeep\n"
    # text with no CR/BS is untouched
    assert analyze.collapse_cr("plain text") == "plain text"


def test_collapse_applied_to_model_input_only():
    tmp, sd = make_session(n_cmds=1, out=b"  0%\r 50%\r100% done\r\n")
    try:
        c = FakeClient()
        analyze.analyze(sd, c, run_ts="RUNC", collapse=True)
        # the map prompt the model saw has the collapsed progress line
        sent = "\n".join(c.prompts)
        assert "100% done" in sent and " 50%" not in sent
        # raw.bin on disk is still byte-exact (collapse never touches it)
        raw = (sd / "panes" / "0-1-1" / "raw.bin").read_bytes()
        assert b" 50%\r100% done" in raw
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_one_bad_chunk_does_not_abort_run():
    # a model-side abort on one chunk must not kill the whole analysis
    tmp, sd = make_session(n_cmds=3, out=b"Z" * 3000)   # fat blocks -> 2+ chunks
    try:
        class Flaky(FakeClient):
            def chat(self, prompt, on_token=None, num_predict=None):
                self.prompts.append(prompt)
                if "CHUNK 2/" in prompt:        # fail only the second map chunk
                    raise analyze.OllamaGenerationError("token repeat limit reached")
                return "ok"
        c = Flaky(num_ctx=6144)
        res = analyze.analyze(sd, c, run_ts="RUNF")
        doc = (sd / "analysis.md").read_text()
        assert res["failed_chunks"] == 1
        assert "could not summarise: 1 of" in doc          # header notes it
        assert (sd / "analysis.md").exists()               # report still written
        # the failed-chunk note propagated into the reduce input
        assert "repeat limit" in (sd / "prompts" / "RUNF" / "reduce.txt").read_text()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_connection_failure_still_aborts_analyze():
    # connectivity failure is NOT swallowed — the user must know the tunnel is down
    tmp, sd = make_session(n_cmds=2)
    try:
        class Down(FakeClient):
            def chat(self, prompt, on_token=None, num_predict=None):
                raise analyze.OllamaError("cannot reach Ollama at ...")
        try:
            analyze.analyze(sd, Down(), run_ts="RUND")
            assert False, "a connectivity failure must propagate"
        except analyze.OllamaError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_non_json_line_surfaces_as_ollama_error():
    def transport(url, body, timeout):
        yield b"<html>502 Bad Gateway</html>\n"      # a proxy error page, not JSON
    c = analyze.OllamaClient(transport=transport)
    try:
        c.chat("p")
        assert False
    except analyze.OllamaError as e:
        assert "non-JSON" in str(e)


def test_analyze_chunk_count_matches_prompts():
    # many big blocks -> multiple chunks -> that many map prompts + 1 reduce
    tmp, sd = make_session(n_cmds=6, out=b"Z" * 2000)
    try:
        # above the reserve floor but small enough that 6 fat blocks need 2+ chunks
        c = FakeClient(num_ctx=6144)
        res = analyze.analyze(sd, c, run_ts="RUN3")
        pdir = sd / "prompts" / "RUN3"
        n_chunks = len(list(pdir.glob("chunk-*.txt")))
        assert n_chunks == res["chunks"] >= 2
        assert len(c.prompts) == n_chunks + 1       # map prompts + one reduce
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import traceback
    names = sorted(k for k, v in list(globals().items())
                   if k.startswith("test_") and callable(v))
    passed = failed = 0
    for name in names:
        try:
            globals()[name]()
            passed += 1
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(names)} total")
    raise SystemExit(1 if failed else 0)
