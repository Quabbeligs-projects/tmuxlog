"""Configuration: built-in defaults overlaid with ~/.tmuxlog/config.toml."""

import copy
import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "paths": {
        "root": "~/.tmuxlog",
    },
    "capture": {
        # "auto" runs the OSC self-test and falls back to "sentinel" if it fails.
        "boundary": "auto",
        "max_block_bytes": 100 * 1024 * 1024,
        "orphan_gap_ms": 500,
        "interactive": [
            "ssh", "telnet", "nc", "ncat", "socat", "msfconsole", "evil-winrm",
            "mysql", "psql", "sqlplus", "smbclient", "ftp", "python", "python3",
            "ipython", "vim", "nvim", "vi", "nano", "less", "man", "top", "htop",
            "watch", "tmux", "screen", "bash", "zsh", "sh",
        ],
    },
    "tmux": {
        # tmux 3.x has no pane-created or after-respawn-pane hook (probed on 3.4
        # and 3.7c); respawn-pane -k keeps the existing pipe, so none is needed.
        "hooks": [
            "after-new-session",
            "after-new-window",
            "after-split-window",
        ],
    },
    # [analyze]: local Ollama only. A legacy [ollama] section is merged in.
    "analyze": {
        "endpoint": "http://127.0.0.1:11434",
        "model": "mistral",
        "num_ctx": 32768,
        "prompt_reserve_tokens": 1536,
        "response_reserve_tokens": 4096,
        "retries": 3,
        "timeout_s": 600,
        # Collapse CR/backspace overwrites (progress bars) in the model input only.
        "collapse_cr": True,
    },
}


def default_config_path() -> Path:
    return Path(os.environ.get("TMUXLOG_ROOT", "~/.tmuxlog")).expanduser() / "config.toml"


def _merge(base: dict, over: dict) -> dict:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(path: Path | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = Path(path) if path else default_config_path()
    if path.exists():
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    if "ollama" in cfg:                       # legacy section name
        cfg["analyze"] = {**cfg["analyze"], **cfg.pop("ollama")}
    if "TMUXLOG_ROOT" in os.environ:
        cfg["paths"]["root"] = os.environ["TMUXLOG_ROOT"]
    cfg["paths"]["root"] = str(Path(cfg["paths"]["root"]).expanduser())
    return cfg
