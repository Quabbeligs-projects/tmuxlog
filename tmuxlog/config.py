import copy
import os
import tomllib
from pathlib import Path

INTERACTIVE = ["ssh", "telnet", "nc", "ncat", "socat", "msfconsole", "evil-winrm",
               "mysql", "psql", "sqlplus", "smbclient", "ftp", "python", "python3",
               "ipython", "vim", "nvim", "vi", "nano", "less", "man", "top", "htop",
               "watch", "tmux", "screen", "bash", "zsh", "sh"]

DEFAULTS = {
    "paths": {"root": "~/.tmuxlog"},
    "capture": {
        "boundary": "auto",
        "max_block_bytes": 100 * 1024 * 1024,
        "orphan_gap_ms": 500,
        "interactive": INTERACTIVE,
    },
    "tmux": {"hooks": ["after-new-session", "after-new-window", "after-split-window"]},
    "analyze": {
        "endpoint": "http://127.0.0.1:11434",
        "allow_remote": False,
        "model": "mistral",
        "num_ctx": 32768,
        "prompt_reserve_tokens": 1024,
        "response_reserve_tokens": 2048,
        "num_predict": 1024,
        "retries": 3,
        "timeout_s": 600,
        "collapse_cr": True,
    },
}


def default_config_path():
    root = os.environ.get("TMUXLOG_ROOT", "~/.tmuxlog")
    return Path(root).expanduser() / "config.toml"


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        path = Path(path)
    else:
        path = default_config_path()
    if path.exists():
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))

    if "ollama" in cfg:
        old = cfg.pop("ollama")
        cfg["analyze"] = {**cfg["analyze"], **old}
    if "TMUXLOG_ROOT" in os.environ:
        cfg["paths"]["root"] = os.environ["TMUXLOG_ROOT"]
    cfg["paths"]["root"] = str(Path(cfg["paths"]["root"]).expanduser())
    return cfg
