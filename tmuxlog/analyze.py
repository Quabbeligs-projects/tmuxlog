"""Local AI analysis of a finalized session via Ollama (127.0.0.1:11434 only).

map/reduce over the session's blocks (docs/DESIGN.md §7, "AI pipeline"):
  - blocks come from finalize.collect_blocks (the same objects finalize writes),
    so a block is never mis-split by re-parsing master.clean.log,
  - pack WHOLE blocks into chunks that fit num_ctx with headroom for the prompt
    template and the response; the ONE exception is a single block bigger than
    the budget, which is split into labelled parts ("block N part M/K"),
  - map pass: each chunk -> a structured summary, fed verbatim (no redaction),
  - reduce pass: the summaries -> one narrative (chronological flow, rabbit-hole
    transitions, findings, next steps),
  - analysis.md gets the sections plus a header (model, time, session, counts);
    --include-raw appends the raw blocks,
  - every prompt is saved verbatim under prompts/<run-ts>/ for replay.

num_ctx is ALWAYS sent explicitly: Ollama otherwise falls back to a small
default and silently truncates the input, the likeliest cause of a wrong-looking
analysis. The endpoint host is validated to be loopback; the only network call
is to Ollama.

The HTTP call sits behind a transport seam (OllamaClient(transport=...)) so the
pipeline is testable without a socket; the default transport uses urllib.
stdlib only.
"""

from __future__ import annotations

import ipaddress
import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from tmuxlog import finalize

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_MODEL = "mistral"
DEFAULT_NUM_CTX = 32768
PROMPT_RESERVE = 1536      # tokens kept for the map/reduce prompt scaffolding
RESPONSE_RESERVE = 4096    # tokens kept for the model's reply


# ---------------------------------------------------------------------------
# token budgeting + chunking (pure, no network)
# ---------------------------------------------------------------------------

def est_tokens(data) -> int:
    """Conservative token estimate without a tokenizer: ~3 bytes/token."""
    n = len(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8", "surrogateescape"))
    return math.ceil(n / 3)


def budget_tokens(num_ctx: int) -> int:
    b = num_ctx - PROMPT_RESERVE - RESPONSE_RESERVE
    if b < 256:
        raise ValueError(f"num_ctx={num_ctx} too small for prompt+response reserve")
    return b


def block_text(b: dict, bid: int) -> str:
    """One block rendered for the model: a labelled header + its clean body."""
    m = b.get("_meta", {})
    head = (f"### Block {bid} [{b['kind']}] pane={b.get('_pane','?')} "
            f"session={m.get('session','?')}")
    if b["kind"] == "command":
        cmd = b.get("cmd") or ""
        if b.get("cmd_parts"):
            cmd += " | " + " | ".join(b["cmd_parts"])
        head += (f"\ncmd: {cmd}\nexit={b.get('exit_code')} status={b.get('status')} "
                 f"cwd={b.get('cwd')} start_ns={b.get('start_ns')}")
    body = b["clean"].decode("utf-8", "replace")
    return head + "\n----\n" + body


def chunk_blocks(items: list[tuple[str, str]], budget: int) -> list[list[dict]]:
    """Pack (label, text) items into chunks of <= budget tokens.

    Never splits an item across chunks, except a single item larger than the
    budget, which becomes its own run of chunks with parts labelled
    "<label> part i/k". Each returned chunk is a list of {label, text} parts.
    """
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_tok = 0
    for label, text in items:
        tok = est_tokens(text)
        if tok > budget:
            if cur:
                chunks.append(cur)
                cur, cur_tok = [], 0
            chunks.extend(_split_oversized(label, text, budget))
            continue
        if cur and cur_tok + tok > budget:
            chunks.append(cur)
            cur, cur_tok = [], 0
        cur.append({"label": label, "text": text})
        cur_tok += tok
    if cur:
        chunks.append(cur)
    return chunks


def _split_oversized(label: str, text: str, budget: int) -> list[list[dict]]:
    data = text.encode("utf-8", "surrogateescape")
    span = budget * 3                                  # bytes per part (3 B/token)
    raw_parts = [data[i:i + span] for i in range(0, len(data), span)]
    k = len(raw_parts)
    out = []
    for i, part in enumerate(raw_parts, 1):
        txt = part.decode("utf-8", "replace")
        out.append([{"label": f"{label} part {i}/{k}", "text": txt}])
    return out


# ---------------------------------------------------------------------------
# Ollama client (HTTP behind a transport seam)
# ---------------------------------------------------------------------------

def _assert_loopback(endpoint: str) -> None:
    host = urlparse(endpoint).hostname or ""
    if host in ("localhost",):
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError(f"tmuxlog analyze refuses a non-loopback endpoint: {endpoint!r}")


def _urllib_transport(url: str, body: bytes, timeout: float):
    """Default transport: POST body, yield response bytes line by line (stream)."""
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)   # nosec - loopback only, validated
    for line in resp:
        yield line


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, model=DEFAULT_MODEL, num_ctx=DEFAULT_NUM_CTX,
                 endpoint=DEFAULT_ENDPOINT, timeout=600.0, retries=3,
                 backoff=0.5, transport=None, sleep=time.sleep):
        _assert_loopback(endpoint)
        self.model = model
        self.num_ctx = num_ctx
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.transport = transport or _urllib_transport
        self._sleep = sleep

    def build_payload(self, prompt: str) -> dict:
        # num_ctx ALWAYS set explicitly — never rely on Ollama's default.
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "options": {"num_ctx": self.num_ctx},
        }

    def chat(self, prompt: str, on_token=None) -> str:
        """Send one prompt, stream the reply, return the full text.

        Retries on connection errors with backoff; on an HTTP error raises
        OllamaError carrying the exact response body.
        """
        url = f"{self.endpoint}/api/chat"
        body = json.dumps(self.build_payload(prompt)).encode()
        last = None
        for attempt in range(self.retries):
            try:
                out = []
                for line in self.transport(url, body, self.timeout):
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if "error" in obj:
                        raise OllamaError(obj["error"])
                    piece = obj.get("message", {}).get("content", "")
                    if piece:
                        out.append(piece)
                        if on_token:
                            on_token(piece)
                return "".join(out)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace") if hasattr(e, "read") else str(e)
                raise OllamaError(f"HTTP {e.code} from Ollama: {detail}") from e
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                last = e
                if attempt < self.retries - 1:
                    self._sleep(self.backoff * (2 ** attempt))
        raise OllamaError(f"cannot reach Ollama at {self.endpoint} after "
                          f"{self.retries} tries: {last}")


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

MAP_PROMPT = """You are analysing part of a penetration-testing terminal log.
Summarise ONLY what this chunk shows. Do not invent or redact anything.
Report, as short sections: commands run, tools used, notable outputs,
findings (hosts, services, creds, versions), and open questions.

--- CHUNK {n}/{m} ---
{chunk}
--- END CHUNK ---"""

REDUCE_PROMPT = """You are writing the final report of a penetration-testing
session from per-chunk summaries, in chronological order. Produce these
sections, and nothing before them:

## Executive Summary
## Chronological Narrative
(explain transitions between lines of investigation, including abandoned ones)
## Findings
## Open Threads / Next Steps

--- SUMMARIES ---
{summaries}
--- END SUMMARIES ---"""


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def analyze(session_dir, client: OllamaClient, include_raw=False,
            progress=None, run_ts=None) -> dict:
    session_dir = Path(session_dir)
    gathered = finalize.collect_blocks(session_dir)
    blocks = gathered["blocks"]

    items = [(f"block {bid}", block_text(b, bid)) for bid, b in enumerate(blocks, 1)]
    chunks = chunk_blocks(items, budget_tokens(client.num_ctx))

    run_ts = run_ts or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prompts_dir = session_dir / "prompts" / run_ts
    prompts_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    m = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        chunk_text = "\n\n".join(f"[{p['label']}]\n{p['text']}" for p in chunk)
        prompt = MAP_PROMPT.format(n=i, m=m, chunk=chunk_text)
        (prompts_dir / f"chunk-{i:03d}.txt").write_text(prompt, encoding="utf-8")
        if progress:
            progress(f"map chunk {i}/{m}")
        summaries.append(f"### Summary of chunk {i}/{m}\n" + client.chat(prompt))

    reduce_prompt = REDUCE_PROMPT.format(summaries="\n\n".join(summaries))
    (prompts_dir / "reduce.txt").write_text(reduce_prompt, encoding="utf-8")
    if progress:
        progress("reduce")
    narrative = client.chat(reduce_prompt)

    gap_bytes = sum(g.get("bytes", 0) for g in gathered["gaps"])
    header = (
        f"# tmuxlog analysis: {session_dir.name}\n\n"
        f"- model: {client.model} (num_ctx={client.num_ctx})\n"
        f"- generated: {datetime.now(timezone.utc).isoformat()}\n"
        f"- blocks: {len(blocks)}   chunks: {m}\n"
        f"- capture gaps: {len(gathered['gaps'])} ({gap_bytes} bytes)\n"
    )
    if gathered["gaps"]:
        header += "\n> WARNING: capture gaps present; the log is INCOMPLETE.\n"
    doc = header + "\n" + narrative + "\n"

    if include_raw:
        doc += "\n\n## Raw blocks\n"
        for bid, b in enumerate(blocks, 1):
            doc += f"\n### Block {bid}\n```\n" + b["clean"].decode("utf-8", "replace") + "\n```\n"

    out = session_dir / "analysis.md"
    out.write_text(doc, encoding="utf-8")
    return {"analysis": str(out), "blocks": len(blocks), "chunks": m,
            "prompts_dir": str(prompts_dir)}
