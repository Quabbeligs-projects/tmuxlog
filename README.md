# tmuxlog

Chronological, per-command log across all
panes and windows of a tmux session.

- **One block per command.** Each block is a command plus its full output and
  metadata: pane, session/window/pane names, start and end timestamps, exit
  code, cwd, user, host.
- **No interleaving.** A block only ever holds its own pane's output, so
  parallel panes never mix inside a block.
- **Ordered by when you pressed Enter**, not by when output arrived. A long
  scan started before a quick lookup is listed first, even if the lookup
  printed first.
- **Raw bytes are always kept.** An ANSI-stripped `master.clean.log` is written
  alongside; it differs from the raw log only in escape sequences.
- **A JSONL event journal** for tooling, plus a sha256 manifest.
- **Optional AI summary** via Ollama: loopback (`127.0.0.1`) by default, or a host
  you control (see [Running Ollama on another machine](#running-ollama-on-another-machine-eg-a-gpu-host)).

> **The logs contain everything verbatim**: credentials, keys, tokens, hashes.
> That is intentional: they are evidence for your private use. tmuxlog does no
> redaction. All state is created `0700` (dirs) / `0600` (files) under
> `~/.tmuxlog/`. Keep it off shared or synced storage.

## Requirements

- Linux (macOS is untested), Python 3.11+ (standard library only, nothing to `pip install`)
- tmux 3.2+ (validated on 3.4 and 3.7c)
- bash 5+ or zsh 5.1+

## Install

```sh
git clone <this repo> ~/tools/tmuxlog
ln -s ~/tools/tmuxlog/bin/tmuxlog ~/.local/bin/tmuxlog     # anywhere on PATH
```

Then add one line to the **end** of your `~/.bashrc` or `~/.zshrc`. tmuxlog never edits these files itself:

```sh
source "$(tmuxlog init-script)"
```

The hooks stay inert until `tmuxlog start-session` arms a pane. They cost no
forks per command and never block or alter a command, even if the log
directory is unwritable.

Optional: `cp config.example.toml ~/.tmuxlog/config.toml` and edit it. Every key
is documented there and every key is optional.

## 60-second demo

```sh
tmuxlog start-session demo        # creates the tmux session if it doesn't exist
tmux attach -t demo
```

In the first pane:

```sh
sleep 3; echo scan-finished       # the "long scan", started first
```

Within 3 seconds, split (`Ctrl-b %`) and in the new pane:

```sh
echo quick-result                 # starts second, finishes first
```

Detach (`Ctrl-b d`), then:

```sh
tmuxlog status --session demo     # per pane: commands, pipe alive, bytes, gaps
tmuxlog stop-session demo         # closes the pipes; captures seal themselves
tmuxlog finalize --session demo
less -R ~/.tmuxlog/sessions/demo/final/master.log      # or master.clean.log as plain text
```

`master.log` lists the `sleep 3; echo scan-finished` block *before*
`echo quick-result`, because it started first. Each block holds only its own
output.

If Ollama runs locally:

```sh
tmuxlog analyze --session demo --model mistral    # -> sessions/demo/analysis.md
```

## Commands

| Command | What it does |
|---|---|
| `start-session [<name>]` | Arms every pane of tmux session `<name>`, creating it detached if needed; inside tmux with no name, arms the current session. Probes tmux once per version to pick the boundary mode. Installs session-scoped hooks, so new windows and splits are armed too. Idempotent. |
| `stop-session <name>` | Removes the hooks, closes the pipes (each capture seals its pane) and disarms the shells. |
| `status [--session]` | Per pane: shells, commands, open commands, pipe `alive` / `sealed` / `server gone`, raw bytes, capture gaps. |
| `finalize [--session]` | Writes `final/master.log`, `master.clean.log`, `events.jsonl` and `manifest.json`, ordered by command start. Safe to re-run at any time; output is replaced atomically. |
| `analyze [--session] [--model] [--include-raw]` | Local Ollama map/reduce over the finalized blocks, written to `analysis.md`. Every prompt is saved under `prompts/<run>/`. By default (`[analyze] collapse_cr = true`) carriage-return/backspace overwrites such as progress bars are collapsed **in the model input only**; `raw.bin` and the master logs are never altered. `--include-raw` appends the raw blocks, escapes preserved. |
| `export --format jsonl [--session]` | Every pane's hook events in timestamp order, on stdout. |
| `events <pane dir>` | Writes one pane's `events.jsonl`. |
| `init-script` | Prints the path to source from your rc file. |

`--session` defaults to the most recently modified session.

## What ends up where

```
~/.tmuxlog/sessions/<name>/
  session.json                 boundary mode, tmux server pid, hooks
  panes/<pane uid>/            one directory per pane, append-only
    raw.bin                    every byte the pane printed, untouched
    arrivals.idx               arrival time of each chunk of raw.bin
    hooks.rec                  command start/end records from the shell hooks
    gaps.jsonl                 only if writes failed (e.g. disk full): exactly what is missing
    sealed, meta.json, capture.pid, boundary
  final/                       written by finalize (regenerable from panes/)
    master.log  master.clean.log  events.jsonl  manifest.json
  analysis.md  prompts/        written by analyze
```

Everything in `final/` is a pure function of `panes/`. After a crash, a killed
tmux server or a power cut, just run `tmuxlog finalize` again.

Block states:
- **`complete`:** the command finished normally.
- **`interrupted`:** it was stopped with Ctrl-C (exit 130).
- **`still_running`:** the pane is still alive. This is normal for `ssh`,
  `msfconsole`, `vim` and nested shells.
- **`unterminated`:** the pane or server died mid-command.

Output that arrives after a command has finished goes into an **orphan** block,
timestamped on arrival (typically a background job). Output before the first
command goes into a **preamble** block.

## Running Ollama on another machine (e.g. a GPU host)

`analyze` talks to `127.0.0.1:11434` by default. If Ollama runs on another
machine, there are two ways to reach it. They differ in what crosses the
network, so pick one deliberately. Remember that the logs contain credentials,
keys and hashes verbatim, and `analyze` sends the session's clean log to the
model.

| | (a) Direct | (b) SSH tunnel |
|---|---|---|
| Log data on the LAN | **plain, unencrypted HTTP** | encrypted inside SSH |
| tmuxlog config | `endpoint` = GPU host, `allow_remote = true` | unchanged (loopback only) |
| Ollama on the GPU host listens on | the LAN (`OLLAMA_HOST=0.0.0.0:11434`) | loopback only (`127.0.0.1:11434`) |
| Pick it when | a trusted host on a trusted network, simplest setup | the network isn't fully trusted, or you can't or won't expose Ollama on the host's LAN interface |

### (a) Direct: simplest, but plain HTTP on the LAN

On the GPU host:

```sh
ollama pull mistral
OLLAMA_HOST=0.0.0.0:11434 ollama serve     # listen on the LAN, not only on loopback
```

If Ollama runs as a systemd service, set the variable there instead
(`sudo systemctl edit ollama`, then add
`Environment="OLLAMA_HOST=0.0.0.0:11434"` under `[Service]`, then restart it).

In tmuxlog's `~/.tmuxlog/config.toml` on the capture box:

```toml
[analyze]
endpoint = "http://<GPU-HOST-IP>:11434"
allow_remote = true
```

Every `analyze` run then prints
`WARNING: sending session data to http://<GPU-HOST-IP>:11434 (allow_remote=on)`.
Anyone who can sniff that network segment can read the log, and Ollama itself
has no authentication. Only use this on a host and network you trust, and
firewall port 11434 on the GPU host to the capture box's address.

### (b) SSH loopback tunnel: keeps strict loopback-only, encrypted

On the GPU host, leave Ollama on its default loopback address
(`OLLAMA_HOST` unset, or `127.0.0.1:11434`) and pull the model:
`ollama pull mistral`.

On the capture box, stop any local Ollama first so port 11434 is free (e.g.
`systemctl stop ollama`, or quit `ollama serve`), then open the tunnel:

```sh
ssh -f -N -L 127.0.0.1:11434:127.0.0.1:11434 USER@GPU-HOST-IP
```

This maps the capture box's `127.0.0.1:11434` to the GPU host's own
`127.0.0.1:11434` over SSH. tmuxlog's config stays unchanged
(`endpoint = "http://127.0.0.1:11434"`, `allow_remote = false`) and `analyze`
stays loopback-only. The log crosses the network only inside the SSH
connection. Close the tunnel afterwards with
`pkill -f 'ssh -f -N -L 127.0.0.1:11434'`, or keep it up with your usual SSH
config (`LocalForward 127.0.0.1:11434 127.0.0.1:11434`).

## Troubleshooting

### 1. Commands in a pane are not logged (unarmed shell)

Symptoms: `status` shows `0 shell(s)` for the pane, or the pane's whole output
appears as a single preamble block.

- Check that the shell sourced the hooks: `type __tl_preexec` must print a
  function. If it doesn't, the `source "$(tmuxlog init-script)"` line is missing
  or not at the end of your rc file. On bash it also must be 5.0 or newer
  (`echo $BASH_VERSION`).
- Check that the pane is armed: `ls ~/.tmuxlog/run/*/` should contain the
  pane's number. Panes of *other* tmux sessions are not armed; run
  `tmuxlog start-session` for that session too.
- A pane created before `start-session` arms at its next command. Nothing needs
  restarting.
- `status` showing `pipe server gone` means the tmux server was restarted. Run
  `tmuxlog start-session <name>` again; old panes stay in the session dir.

### 2. `start-session` says a pane is "already piped by something else"

tmux allows one `pipe-pane` per pane, and another tool (for example the
tmux-logging plugin) holds it. tmuxlog does not clobber it. Close that pipe,
then re-run (`start-session` is idempotent and skips panes it already captures):

```sh
tmux pipe-pane -t %3          # no command = close the existing pipe
tmuxlog start-session <name>
```

### 3. Everything inside a nested tmux is one big block

This is a known limitation. Shells inside a nested tmux belong to the inner
server, so the outer tmuxlog never arms them, and the inner tmux swallows
boundary marks anyway. The whole inner session is logged as one opaque block
(raw bytes kept), sealed `still_running` until it exits. To get per-command
blocks, run `tmuxlog start-session` on the inner server as well; it gets its own
session directory.

### 4. bash: a background command's output is an orphan with no command

This is a known limitation of bash. Its DEBUG trap does not fire for lines that
run *only* in a subshell:
- `(cmd)` and `(cmd) &`
- async lists `a && b &`
- async groups `{ a; b; } &`

These commands get no block of their own. Their output is still preserved, in
an orphan block, but it isn't attributed to the command.

These are captured normally:
- plain background commands (`cmd &`, `nohup cmd &`)
- `sh -c '…' &`
- pipelines, `if`/`for`/`{ }`
- everything in zsh

### 5. `analyze` fails (Ollama down, wrong model, odd output)

| Message | Fix |
|---|---|
| `Ollama error: cannot reach Ollama at http://127.0.0.1:11434 after 3 tries` | Start it: `ollama serve`. Retries and timeout are under `[analyze]`. |
| `Ollama error: HTTP 404 from Ollama: … model "x" not found …` | `ollama pull <model>`, or pass `--model` / set `[analyze] model`. |
| `refuses a non-loopback endpoint` | By default only `127.0.0.1`, `::1` or `localhost` are accepted. For a GPU host, use an SSH tunnel or `allow_remote` (see [Running Ollama on another machine](#running-ollama-on-another-machine-eg-a-gpu-host)). |
| `analyze` is very slow, or seems stuck on one chunk | Run `ollama ps` while it works. Anything other than `100% GPU` (e.g. `45%/55% CPU/GPU`) means `num_ctx` plus the model don't fit in free VRAM and layers spilled to the CPU: lower `[analyze] num_ctx` (8192 is safe on an 8 GB card). Each chunk summary is capped by `num_predict` (default 1024 tokens) and the final reduce at 2048, so a looping model can't generate forever. To watch progress, follow the Ollama server log: `journalctl -u ollama -f` for the Linux service, the terminal running `ollama serve`, or `~/.ollama/logs/server.log` for the desktop app. A climbing `n_gen` means it is still generating, and `tg` t/s is the generation speed. |
| Analysis looks wrong or ignores parts of the log | Check `prompts/<run>/chunk-NNN.txt`: these are the exact prompts sent, after `collapse_cr` (set `collapse_cr = false` to send progress-bar output uncollapsed). Lower `num_ctx` if the model can't handle 32k; tmuxlog always sends `num_ctx` explicitly and packs whole blocks to fit it. |

`analyze` exits non-zero with Ollama's exact error message.

## Known limitations (V1)

- **Nested tmux:** opaque block (see troubleshooting 3).
- **bash lines that run only in a subshell are not attributed** to a command
  (output kept, as an orphan block): bare subshells `(cmd)`, `(cmd) &` and async
  lists/groups `a && b &`, `{ a; b; } &`. Plain `cmd &`, `nohup … &` and
  `sh -c '…' &` are attributed, as is everything in zsh (see troubleshooting 4).
- **bash timestamp resolution:** microseconds (`$EPOCHREALTIME`), stored as
  ns = µs × 1000. zsh records true nanoseconds.
- **Background output during a foreground command in the same pane:** it lands
  in the foreground block. A pane is one stream and can't be separated in-band.
- **Sentinel fallback mode:** a mark that wraps at the right screen edge can
  leave its tail visible. Content is unaffected.

The design, file formats and every failure mode are documented in
[docs/DESIGN.md](docs/DESIGN.md).

## Tests

```sh
pip install pytest            # test-only dependency
python3 -m pytest tests -v
```

The suite drives real interactive bash/zsh through a pty and real headless
tmux servers (`tmux -L …`), using fake data only. It needs `tmux`, `bash` and
`zsh` on PATH; zsh tests skip if zsh is missing. `analyze` is tested against a
mock Ollama on loopback.
