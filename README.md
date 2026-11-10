# TMUXLOG

**TMUXLOG** is a chronological logging tool, across all panes and windows of a tmux session. This tool can help you organize yourself across multiple sessions of whatever you’re doing in tmux.

I personally use this for pentesting, red teams, bug bounties, home labs, CTF’s, etc…, even though I do take notes during these activities, sometimes I do forget some stuff, or I‘m not that clear in my notes and don‘t understand what the fuck I wrote down when I look at them later on. Things tend to get really messy really quickly.

So I decided to look for a tool that could log tmux sessions, but every tool I found had interweaving problems, if multiple command outputs were printing at the same time, the log would become a mess and you couldn’t read anything, so then I decided I‘ll just do it myself and it has helped me immensely. I just look at the clean logs, as they contain absolutely everything of a session.

I recommend keeping your work segmented in different sessions, so for example, I make different sessions for passive enumeration, active enumeration, etc…, keeping the logs short helps a lot, if you have to find something specifically later on, or if you just want to look at a specific segments output to refresh your memory. There is no problem in having longer sessions, I just recommend to actually segment your work, for quality of life, as going through one giant log can be hell.

There is an `analyze` feature, which you can setup, if you want, where a local model can analyze your clean log for you and give you a session report.

This feature is purely experimental and I would not trust it, especially if you don‘t have a good GPU, as it gets really bad with small context windows and small models. I myself do not use this feature, I just look at my clean logs myself and analyze them on my own, as my activities are too complex for this small of a context window and model. When I tried using it a few times, it did get some stuff right, but it also misinterpreted a lot of stuff, got confused, concentrated on insignificant stuff or just spat straight up bs.

Also I only have basic knowledge, when it comes to ML, etc…, so I had to use claude code for this feature, so the way this feature is setup is probably also not helping at all, but in general, I do not recommend using it, if you really need a model to analyze your logs, you‘d be better off asking one of the big models to analyze your clean logs, but I have to warn you that the clean log contains the direct input and output of your commands, there is absolutely no sanitisation being done, so you have to be careful to not misshandle potential sensitive data in those logs.

But I recommend just analyzing the logs on your own.

> **The logs contain everything verbatim**: credentials, keys, tokens, hashes.
> That is intentional, they are evidence for your private use. tmuxlog does no
> redaction. All state is created `0700` (dirs) / `0600` (files) under
> `~/.tmuxlog/`. Keep it off shared or synced storage.
> 
> tmuxlog is licensed under GPL-3.0, see [LICENSE](LICENSE).
> Copyright (C) 2026 Quabbelig
> 
> You're welcome to try and read my code but I have to warn you that I barely comment, the only file with understandable comments is `analyze`, because claude code made it, you're on your own for the rest, good luck trying to understand my mess...

## Requirements

- Linux (macOS and Windows are untested), Python 3.11+
- tmux 3.2+ (tested on 3.4 and 3.7c)
- bash 5+ or zsh 5.1+

## Install

```sh
git clone https://github.com/Quabbeligs-projects/tmuxlog.git ~/tools/tmuxlog
ln -s ~/tools/tmuxlog/bin/tmuxlog ~/.local/bin/tmuxlog
```

`~/.local/bin` has to exist and be on your `PATH` for the `tmuxlog` command to
work. If it isn't already:

```sh
mkdir -p ~/.local/bin
```

If it's not on `PATH` yet, add this to your `~/.bashrc` or `~/.zshrc`:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Then add one line to the end of your `~/.bashrc` or `~/.zshrc`.

```sh
source "$(tmuxlog init-script)"
```

The hooks stay inert until `tmuxlog start-session` arms a pane. They cost no
forks per command and never block or alter a command.

Optionally create `~/.tmuxlog`, copy the example config into it, and edit it.
Every key is documented there and every key is optional.

```sh
mkdir -p ~/.tmuxlog
cp ~/tools/tmuxlog/config.example.toml ~/.tmuxlog/config.toml
```

## Commands

| Command | What it does |
|---|---|
| `start-session <name>` | Arms every pane of tmux session `<name>`, creating it detached if needed; inside tmux with no name, arms the current session. Probes tmux once per version to pick the boundary mode. Installs session-scoped hooks, so new windows and splits are armed too. Idempotent. |
| `stop-session <name>` | Removes the hooks, closes the pipes (each capture seals its pane) and disarms the shells. |
| `status --session <name>` | Per pane: shells, commands, open commands, pipe `alive` / `sealed` / `server gone`, raw bytes, capture gaps. |
| `finalize --session <name>` | Writes `final/master.log`, `master.clean.log`, `events.jsonl` and `manifest.json`, ordered by command start. Safe to re-run at any time; output is replaced atomically. |
| `analyze --session <name> --model --include-raw` | Local Ollama map/reduce over the finalized blocks, written to `analysis.md`. Every prompt is saved under `prompts/<run>/`. By default (`[analyze] collapse_cr = true`) carriage-return/backspace overwrites such as progress bars are collapsed **in the model input only**; `raw.bin` and the master logs are never altered. `--include-raw` appends the raw blocks, escapes preserved. |
| `export --format jsonl --session <name>` | Every pane's hook events in timestamp order, on stdout. |
| `events <pane dir>` | Writes one pane's `events.jsonl`. |
| `init-script` | Prints the path to source from your rc file. |

`--session` without `<name>` behind it defaults to the most recently modified session.

## What ends up where

```
~/.tmuxlog/sessions/<name>/
  session.json                 boundary mode, tmux server pid, hooks
  panes/<pane uid>/            one directory per pane
    raw.bin                    every byte the pane printed
    arrivals.idx               arrival time of each chunk of raw.bin
    hooks.rec                  command start/end records from the shell hooks
    gaps.jsonl                 only if writes failed (e.g. disk full), what‘s missing
    sealed, meta.json, capture.pid, boundary
  final/                       written by finalize (regenerable from panes/)
    master.log  master.clean.log  events.jsonl  manifest.json
  analysis.md  prompts/        written by analyze
```

Block states:
- **`complete`:** the command finished normally.
- **`interrupted`:** it was stopped with Ctrl-C (exit 130).
- **`still_running`:** the pane is still alive. This is normal for `ssh`,
  `msfconsole`, `vim` and nested shells.
- **`unterminated`:** the pane or server died mid-command.

Output that arrives after a command has finished goes into an **orphan** block,
timestamped on arrival (typically a background job). Output before the first
command goes into a **preamble** block.

## Output files

`master.log` is the raw sha256 session log.

`master.clean.log` is the legible plain text session log.

`events.jsonl` has one line per command and one record per block.

`manifest.json` has every block with its timestamps, exit code,
byte range and sha256, the per-file hashes, and any capture gaps.

`analysis.md` is the models session report.

`prompts/` contains all the prompts for the session report.

## Running Ollama on another machine (e.g. a GPU host)

`analyze` uses `127.0.0.1:11434` by default. If Ollama runs on another
machine, there are two ways to reach it. Option a is easiest, it’s just direct, plain unencrypted HTTP, use it when you can trust the network. Option b is harder, using an SSH tunnel to encrypt the data, use it when the network isn't fully trusted, or you can't or won't expose Ollama on the host's LAN interface. Remember that the logs contain credentials,
keys and hashes verbatim, and `analyze` sends the session's clean log to the model.

### (a) Direct

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
Anyone can read the log, and Ollama itself
has no authentication. Only use this on a host and network you trust, and
firewall port 11434 on the GPU host to the capture box's address.

### (b) SSH loopback tunnel

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
