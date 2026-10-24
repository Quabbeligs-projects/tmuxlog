"""State locations and private (0700/0600) file helpers.

Logs intentionally contain secrets verbatim, so every directory tmuxlog
creates is 0700 and every file 0600, independent of the caller's umask.
"""

import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600

SHELL_DIR = Path(__file__).resolve().parent / "shell"
INIT_SCRIPT = SHELL_DIR / "tmuxlog-init.sh"


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def open_private(path: Path, mode: str = "ab"):
    """Open a file for writing, creating it 0600 if it does not exist."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(path, flags, FILE_MODE)
    os.fchmod(fd, FILE_MODE)
    return os.fdopen(fd, mode)


def write_private_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` via a 0600 temp file and rename."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open_private(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def session_dir(root: Path, name: str) -> Path:
    return Path(root) / "sessions" / name


def run_pointer(root: Path, server_pid: int | str, pane_id: str) -> Path:
    """Arm pointer read by the shell hooks: <root>/run/<server pid>/<pane number>."""
    return Path(root) / "run" / str(server_pid) / pane_id.lstrip("%")
