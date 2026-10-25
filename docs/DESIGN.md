# tmuxlog — design (draft for review)

## 0. Validated facts (tmux 3.4 and 3.7c, bash 5.2; `python3 -m tmuxlog.probe` re-runs the probe)

| Probe | Result | Consequence |
|---|---|---|
| `set-hook -g pane-created …` | `invalid option: pane-created` | **No such hook in 3.4.** Use `after-new-session`, `after-new-window`, `after-split-window` + enumerate existing panes at `start-session`. `after-respawn-pane` is also invalid (3.4 and 3.7c), but `respawn-pane -k` keeps the pipe open, so the capture process continues. |
| `printf '\e]133;C;id=42\a…'` and a private `\e]6973;…\e\\` in a pane | Both arrive in `pipe-pane -O` output **byte-exact** | In-band OSC marks work; no printable sentinel needed on 3.4. |
| `capture-pane` after the same printf | Screen shows `XY` — the OSC is swallowed | Marks are invisible to the user. |
| `kill-pane` | `pane-exited` does **not** fire; the pipe command **does** get EOF | Capture process seals the pane on stdin EOF; no reliance on hooks for closure. |
| Typed command line | Echoed into the pipe before the command's output | De-dup needed (§4). |

## 1. Language: Python 3.11+ stdlib only
`tomllib` (3.11) for config, `urllib`/`http.client` for Ollama, `hashlib`, `json`. No build step, auditable on the attack box, present on Kali by default. Hot paths are the shell hooks (pure shell, no Python) and the capture process (one long-lived Python per pane doing `read()`→`write()`), so Go's speed buys nothing measurable.

## 2. In-band boundary mechanism
The hooks print a **private OSC** (not plain OSC 133, which prompt frameworks, remote shells and `cat`-ed logs also emit):
`ESC ] 6973 ; tmuxlog ; <kind> ; <nonce> ; <seq> [; <exit>] BEL`, with kind ∈ `A` (prompt start), `B` (prompt end, embedded in PS1 via `\[…\]` / `%{…%}`), `C` (pre-exec, the Enter moment), `D` (command done).
`nonce` = random per shell instance (`$RANDOM$$${EPOCHREALTIME}`, no fork); the splitter accepts a mark only if its nonce was registered in that pane's hook journal, so replaying an old log (`cat master.log`) or remote output can't fake boundaries. Optional `emit_osc133 = true` also emits standard 133 for tmux 3.4's `next-prompt` navigation.
**Fallback** (`boundary = "sentinel"`, auto-selected if `tmuxlog.probe` finds the OSC not passed byte-exact or not invisible, e.g. an older tmux): `@@TMUXLOG;<payload>@@` followed by `ESC[<width>D ESC[K`, i.e. printed, then cursor-left over exactly its own width and erased, so it is never left on screen and is safe at the end of PS1 (a `\r` there would erase the prompt). Same payloads as the OSC. The hooks read the style once at arm time from `<pane>/boundary`; the splitter strips both forms. Limitation: if a mark wraps at the right margin (cursor-left cannot cross lines) its tail stays visible; content is unaffected. **Neither form survives a nested tmux** (probed on 3.4 and 3.7c: the inner tmux swallows the OSC and coalesces the draw-then-erase sentinel away), see §8.

## 3. File layout (dirs 0700, files 0600, `umask 077` inside every tmuxlog process)
```
~/.tmuxlog/config.toml
~/.tmuxlog/run/<tmux-server-pid>/<pane_id>        # "armed" pointer: contains session dir
~/.tmuxlog/sessions/<name>/
  session.json                 # tmux socket, server pid, created_ns, hook names installed
  panes/<pane_uid>/            # pane_uid = <pane_id>-<server_pid>-<first_seen_ns> (%ids are reused)
    meta.json                  # pane/window/session ids+names snapshot, tty, created_ns
    hooks.rec                  # written ONLY by the shell hooks, append-only (§5)
    raw.bin                    # written ONLY by the capture process: verbatim pipe-pane bytes
    arrivals.idx               # 16-byte records (arrival_ns, raw offset) per read()
    sealed                     # created on EOF (pane closed / server died) with ns
  final/ master.log  master.clean.log  events.jsonl  manifest.json
  prompts/<run-ts>/chunk-NNN.txt, reduce.txt
  analysis.md
```
No central daemon. Each pane has exactly two writers on disjoint files; everything in `final/` is a pure function of `panes/*` and can be regenerated any time.

## 4. Data model
**Hook record** (`hooks.rec`, shell side; `<len>:<v>` fields are byte-length-prefixed via `LC_ALL=C ${#v}`, so values may hold spaces, colons, newlines; parsed by `tmuxlog/journal.py`, which is the format spec):
```
I <nonce> <ts_ns> <pid> <shell> [<shell_version> <shlvl>]              arm time
S <seq> <ts_ns> <nonce> <shlvl> [<src>] <len>:<cmd> <len>:<cwd> <len>:<user> <len>:<host>
T <seq> <ts_ns> <nonce> <part> <len>:<cmd>                              same (nonce, seq) as its S
E <seq> <ts_ns> <nonce> <exit>
```
`src`: `hist` (bash, full line from history), `bc` (bash, `$BASH_COMMAND` = first simple command), `px` (zsh preexec, always the full line). `T` exists only after an `S` with `src=bc` (e.g. space-prefixed line under `HISTCONTROL=ignorespace`): one per later DEBUG-trap stage of the same line, `part` = 1-based index after the first, capped at 32 per line. All stages share one seq, hence one block; finalize joins them with a visible separator and flags the block, since the shell operators (`|`, `&&`, `;`) are not recoverable from the trap.
**Block** (assembled by finalize): `{block_id, kind: command|preamble|orphan, pane_uid, pane_id, session, window_index/name, pane_index/title, start_ns, end_ns, exit_code, status: complete|interrupted|still_running|unterminated, cmd, cwd, user, host, shell, depth, raw_offsets:[a,b], bytes_total, bytes_emitted, truncated, echo_dedup: matched|kept, sha256_raw, sha256_clean}`.
**events.jsonl**: `cmd_start`, `cmd_end` per hook record, then one `block` record per block (above), in final order.

**Segmenting `raw.bin`** per nonce/seq: `C(n)…D(n)` = block body; `A…B` = prompt (dropped from blocks, kept in raw.bin); `B…C` = typed echo — compared, after ANSI/CR strip, with the hook's `cmd`; if it matches it is dropped, if not (completion redraws, bg output while typing) it goes to an orphan block. `D…A` and anything after `D` that isn't prompt/echo → **orphan block**, timestamped by `arrivals.idx`. Bytes before the first mark → **preamble** block at `created_ns`. Marks themselves are excluded from master.log but stay in raw.bin; offsets + sha256 in the manifest let anyone verify a block against raw.bin.

## 5. Shell hooks (bash 5+, zsh)
`source tmuxlog-init.sh` (bash: `trap DEBUG` with an in-command guard + `PROMPT_COMMAND`; zsh: `add-zsh-hook preexec/precmd`). Per command, all builtins, no fork: check arm file with `[[ -r ]]`/`read -r` at each prompt, and at preexec while unarmed (lets `start-session`/`stop-session` arm/disarm already-running shells, and closes the new-pane race), `printf … >&$fd` to a fd opened once with `exec {fd}>>hooks.rec`, `printf` the OSC to the tty. Regular-file `O_APPEND` writes < 4 KiB never block on any backend; any failure (`fd` gone, disk full) → the hook silently disarms itself; it never `wait`s and never touches the command. Timestamps: zsh `$epochtime` (ns), bash `$EPOCHREALTIME` (µs ×1000 — bash has no ns clock; documented). Full command: zsh preexec `$1` (multi-line, heredoc body included); bash `history 1` (full multi-line entry), falling back to `$BASH_COMMAND` if history is off.

## 6. Finalize algorithm
1. For each pane dir: parse hooks.rec (tolerating a torn last line), scan raw.bin for marks, build segments (§4), map arrivals for orphans.
2. Seal open blocks: has `S` but no `E` → `still_running` if the pane is alive (tmux query), else `unterminated` (exit null); `E` without `D` mark or vice-versa → use whichever exists.
3. Sort all blocks by `(start_ns, numeric pane_id, session_name)`.
4. Write master.log (raw bytes per block between header/footer lines), master.clean.log (same, escape sequences stripped), events.jsonl, manifest.json (sha256 per block and per final file). Writes go to temp files + `rename`, so a crash mid-finalize leaves the previous output intact. Truncation (default 100 MB/block, master files only): head + tail kept, with marker `[tmuxlog: TRUNCATED — showing N of TOTAL bytes; full block = panes/<uid>/raw.bin [a,b) sha256=…]`.

## 7. Analyze (Ollama, 127.0.0.1:11434 only — endpoint host is validated to be loopback)
Token estimate = bytes/3 (conservative, no tokenizer). Budget = `num_ctx − prompt_template − response_reserve` (32768 − ~1.5k − 4k). Greedily pack whole blocks in chronological order. **`num_ctx` is always sent explicitly** — Ollama otherwise silently uses a small default and truncates the input, the most likely cause of a wrong-looking analysis. Map → per-chunk structured summary (JSON-ish sections); reduce → narrative; if the summaries themselves exceed budget, reduce hierarchically. Streaming `/api/chat`, progress `chunk N/M`, 3 retries with backoff on connection errors, otherwise print Ollama's exact error body and exit non-zero. Every prompt saved verbatim to `prompts/`.

## 8. Edge cases
| Case | Behavior |
|---|---|
| ssh / msfconsole / vim / nested shell without hooks / **nested tmux** | One block stays open; finalize seals it `still_running` (or `unterminated` if the pane is gone). Commands on `interactive` list are tagged `interactive: true`. |
| Nested tmux (known limitation) | Shells inside it see the inner server's `$TMUX`, so the outer tmuxlog never arms them, and the inner tmux would swallow their marks anyway. The whole inner tmux is one opaque outer block (raw bytes kept, not segmented), sealed `still_running` until it exits. Run `tmuxlog start-session` on the inner server to log it there. Possible future enhancement: wrap marks in tmux DCS passthrough (`ESC P tmux; …`), which reaches the outer pipe intact when the inner tmux has `allow-passthrough on`. |
| bash line that runs only in a subshell: `(cmd)`, `(cmd) &`, and async lists/groups `a && b &`, `{ a; b; } &` (known V1 limitation) | bash's DEBUG trap does not fire in the parent for these, so no `S`/`C`: the output is preserved (raw.bin, an orphan block) but not attributed to a command. zsh is unaffected; in bash, plain background commands (`cmd &`, `nohup … &`), `{ …; }`, `(a) \| b`, `if`/`for`, `[[ ]] &&` are captured. Not fixed in V1: `PS0` sees every line, but its expansion side effects do not persist, so an `S` could only be written at precmd, later than the real Enter moment, which would corrupt start-time ordering. Possible future enhancement: have `PS0` write the record itself to an fd. |
| Nested shell that sources the init | New nonce, `depth = SHLVL delta`; inner commands become their own blocks; outer block keeps the remaining bytes. |
| `cmd &`, nohup, disown | C and D fire at once; later output → orphan block(s), one per arrival burst (gap > 500 ms), at arrival ns. |
| bg output *while* a foreground command runs in the same pane | **Cannot be separated in-band** (one pty stream) — lands in the foreground block. Documented limitation. |
| Ctrl-C | precmd runs with `$?` = 130 in both shells → `interrupted`, exit 130. Ctrl-C at empty prompt: no open command → no D. |
| Clear screen / cursor moves / binary | raw untouched; clean = escape sequences removed only; invalid UTF-8 bytes passed through. |
| Pane closed, server killed, `kill -9` shell | Capture gets EOF → `sealed`; open blocks sealed `unterminated` at finalize. If capture itself is killed, the next `finalize` still works from the files on disk. |
| Preamble | Preamble block per pane at `created_ns`. |
| Same-ns tie | `(start_ns, pane_id, session_name)`. |
| Pane reuse / server restart | `%id`s are reused → pane_uid includes server pid + first-seen ns. |

## 9. Failure modes & recovery
| Failure | Detection | Recovery |
|---|---|---|
| Hook file unwritable / disk full | write fails | hook disarms silently; command unaffected; `tmuxlog status` reports pane as unarmed |
| Capture process dies | `pipe-pane` reports no pipe; raw.bin stops growing | `tmuxlog start-session` (idempotent, `pipe-pane -o`) re-attaches; gap recorded in arrivals |
| Shell not sourcing init | raw bytes but no hook records | whole pane = one `preamble`/unhooked block; `status` warns |
| OSC dropped or visible (older tmux) | `tmuxlog.probe` | auto-switch to sentinel mode |
| bash subshell-only / async-list line | — | output kept as an orphan block; command not attributed (see §8) |
| New pane's first prompt drawn before its arm pointer exists | — | unarmed hooks re-check the pointer at preexec (one stat, armed shells skip it); `I` is written before the first `S` |
| Torn record from crash | parse error on last line | ignored, counted in manifest `warnings` |
| Finalize crash | — | atomic rename; rerun |
| Ollama down / wrong model / ctx overflow | HTTP error | 3 retries, then exact error; prompts kept for replay |
