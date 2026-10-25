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
PROMPT_RESERVE = 1024      # tokens kept for the map/reduce prompt scaffolding
RESPONSE_RESERVE = 2048    # tokens kept for the model's reply (>= largest num_predict)
DEFAULT_NUM_PREDICT = 1024  # hard cap on a map summary's length — without this a
                            # model can loop and generate thousands of tokens
REDUCE_NUM_PREDICT = 2048   # the final narrative is allowed to be longer


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


def collapse_cr(text: str) -> str:
    """Collapse carriage-return and backspace overwrites to their final line state.

    A progress bar (gobuster, hydra) writes the same line hundreds of times with
    a leading ``\\r``; fed verbatim to the model that is thousands of near-identical
    lines wasting context. This simulates a cursor per physical line: ``\\r`` returns
    to column 0 and later characters overwrite, ``\\x08`` steps back one column.
    Applied ONLY to the analyze input (never to raw.bin or master.clean.log).
    """
    if "\r" not in text and "\x08" not in text:
        return text
    out = []
    for line in text.split("\n"):
        if "\r" not in line and "\x08" not in line:
            out.append(line)
            continue
        buf, col = [], 0
        for ch in line:
            if ch == "\r":
                col = 0
            elif ch == "\x08":
                col = max(0, col - 1)
            else:
                if col < len(buf):
                    buf[col] = ch
                else:
                    buf.append(ch)
                col += 1
        out.append("".join(buf))
    return "\n".join(out)


def block_text(b: dict, bid: int, collapse: bool = True) -> str:
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
    if collapse:
        body = collapse_cr(body)
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
    """Connectivity or protocol failure — fatal to the run (e.g. tunnel down)."""


class OllamaGenerationError(OllamaError):
    """A single generation failed on the model side (e.g. repeat-limit abort).

    Per-chunk and recoverable: the analysis skips that chunk and carries on,
    rather than discarding every other chunk's work.
    """


class OllamaClient:
    def __init__(self, model=DEFAULT_MODEL, num_ctx=DEFAULT_NUM_CTX,
                 endpoint=DEFAULT_ENDPOINT, timeout=600.0, retries=3,
                 backoff=0.5, transport=None, sleep=time.sleep,
                 allow_remote=False, num_predict=DEFAULT_NUM_PREDICT):
        # Loopback-only by default: a pentest log holds secrets, so analyze must
        # not send it off-box unless the user explicitly opts in. allow_remote
        # lets the endpoint be a host the user controls (e.g. their own GPU box);
        # the CLI warns loudly when it is set.
        if not allow_remote:
            _assert_loopback(endpoint)
        self.model = model
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.transport = transport or _urllib_transport
        self._sleep = sleep

    def build_payload(self, prompt: str, num_predict=None) -> dict:
        # num_ctx ALWAYS set explicitly — never rely on Ollama's default.
        # num_predict caps the reply so a looping model can't run away.
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict if num_predict is None else num_predict,
            },
        }

    def chat(self, prompt: str, on_token=None, num_predict=None) -> str:
        """Send one prompt, stream the reply, return the full text.

        Retries on connection errors with backoff; on an HTTP error raises
        OllamaError carrying the exact response body.
        """
        url = f"{self.endpoint}/api/chat"
        body = json.dumps(self.build_payload(prompt, num_predict)).encode()
        last = None
        for attempt in range(self.retries):
            try:
                out = []
                for line in self.transport(url, body, self.timeout):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        raise OllamaError(
                            f"non-JSON line from Ollama: {line[:200]!r}")
                    if "error" in obj:
                        # a model-side failure for THIS request (repeat-limit
                        # abort, context error): recoverable per-chunk, not fatal
                        raise OllamaGenerationError(obj["error"])
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
# reduce (hierarchical: never overflow num_ctx)
# ---------------------------------------------------------------------------

def _safe_chat(client, prompt: str, num_predict: int) -> tuple[str, bool]:
    """Call the model, but turn a per-request generation failure into a note.

    Returns (text, ok). A model-side abort (OllamaGenerationError) yields
    (note, False) so the caller can carry on; connectivity failures (OllamaError)
    still propagate and stop the run.
    """
    try:
        return client.chat(prompt, num_predict=num_predict), True
    except OllamaGenerationError as e:
        return f"[model error: {e}]", False


def _pack_text(parts: list[str], budget: int) -> list[list[str]]:
    groups, cur, cur_tok = [], [], 0
    for p in parts:
        t = est_tokens(p)
        if cur and cur_tok + t > budget:
            groups.append(cur)
            cur, cur_tok = [], 0
        cur.append(p)
        cur_tok += t
    if cur:
        groups.append(cur)
    return groups or [[]]


def reduce_pass(client, summaries: list[str], prompts_dir: Path, progress=None,
                reduce_num_predict=REDUCE_NUM_PREDICT) -> str:
    """Combine chunk summaries into one narrative, in levels when they don't fit.

    If all summaries fit the model's input budget they go into a single reduce.
    Otherwise they are grouped to fit, each group reduced, and the results
    reduced again — so an arbitrarily large session never overflows num_ctx. The
    final prompt is always saved as reduce.txt.
    """
    budget = budget_tokens(client.num_ctx)
    parts = list(summaries)
    level = 0
    while True:
        groups = _pack_text(parts, budget)
        if len(groups) == 1 or len(groups) >= len(parts):
            # one group fits, or grouping can't shrink the count further: do the
            # final reduce. If even that fails on the model side, fall back to
            # the concatenated summaries so the report still has content.
            joined = "\n\n".join(groups[0] if len(groups) == 1 else parts)
            prompt = REDUCE_PROMPT.format(summaries=joined)
            (prompts_dir / "reduce.txt").write_text(prompt, encoding="utf-8")
            if progress:
                progress("reduce")
            text, ok = _safe_chat(client, prompt, reduce_num_predict)
            if ok:
                return text
            return ("## Findings\n\n_(The final synthesis step failed on the model "
                    f"side: {text}. The chunk summaries are included below.)_\n\n" + joined)
        level += 1
        reduced = []
        for i, g in enumerate(groups, 1):
            prompt = REDUCE_PROMPT.format(summaries="\n\n".join(g))
            (prompts_dir / f"reduce-L{level}-{i:03d}.txt").write_text(prompt, encoding="utf-8")
            if progress:
                progress(f"reduce level {level} group {i}/{len(groups)}")
            text, _ = _safe_chat(client, prompt, reduce_num_predict)
            reduced.append(text)
        parts = reduced


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def analyze(session_dir, client: OllamaClient, include_raw=False,
            progress=None, run_ts=None, collapse=True) -> dict:
    session_dir = Path(session_dir)
    gathered = finalize.collect_blocks(session_dir)
    blocks = gathered["blocks"]

    items = [(f"block {bid}", block_text(b, bid, collapse)) for bid, b in enumerate(blocks, 1)]
    chunks = chunk_blocks(items, budget_tokens(client.num_ctx))

    run_ts = run_ts or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prompts_dir = session_dir / "prompts" / run_ts
    prompts_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    failed = 0
    m = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        chunk_text = "\n\n".join(f"[{p['label']}]\n{p['text']}" for p in chunk)
        prompt = MAP_PROMPT.format(n=i, m=m, chunk=chunk_text)
        (prompts_dir / f"chunk-{i:03d}.txt").write_text(prompt, encoding="utf-8")
        if progress:
            progress(f"map chunk {i}/{m}")
        text, ok = _safe_chat(client, prompt, client.num_predict)
        if not ok:
            failed += 1
            labels = ", ".join(p["label"] for p in chunk)
            text += (f"\n(This chunk could not be summarised by the model; it held "
                     f"{labels}. See prompts/<run>/chunk-{i:03d}.txt for the input.)")
            if progress:
                progress(f"chunk {i}/{m} skipped: {text.splitlines()[0]}")
        summaries.append(f"### Summary of chunk {i}/{m}\n" + text)

    narrative = reduce_pass(client, summaries, prompts_dir, progress)

    gap_bytes = sum(g.get("bytes", 0) for g in gathered["gaps"])
    raw_bytes = sum(len(b["raw"]) for b in blocks)
    clean_bytes = sum(len(b["clean"]) for b in blocks)
    header = (
        f"# tmuxlog analysis: {session_dir.name}\n\n"
        f"- model: {client.model} (num_ctx={client.num_ctx})\n"
        f"- generated: {datetime.now(timezone.utc).isoformat()}\n"
        f"- blocks: {len(blocks)}   chunks: {m}\n"
        f"- bytes: raw={raw_bytes}  clean={clean_bytes}\n"
        f"- capture gaps: {len(gathered['gaps'])} ({gap_bytes} bytes)\n"
    )
    if failed:
        header += f"- chunks the model could not summarise: {failed} of {m}\n"
    if gathered["gaps"]:
        header += "\n> WARNING: capture gaps present; the log is INCOMPLETE.\n"
    doc = header + "\n" + narrative + "\n"

    if include_raw:
        # the RAW blocks for evidence (ANSI/binary preserved via surrogateescape),
        # not the clean rendering — that is the point of --include-raw
        doc += "\n\n## Raw blocks\n"
        for bid, b in enumerate(blocks, 1):
            doc += f"\n### Block {bid}\n```\n" + b["raw"].decode("utf-8", "surrogateescape") + "\n```\n"

    out = session_dir / "analysis.md"
    out.write_text(doc, encoding="utf-8")
    return {"analysis": str(out), "blocks": len(blocks), "chunks": m,
            "failed_chunks": failed, "prompts_dir": str(prompts_dir)}
