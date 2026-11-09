import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600

SHELL_DIR = Path(__file__).resolve().parent / "shell"
INIT_SCRIPT = SHELL_DIR / "tmuxlog-init.sh"


def ensure_dir(path):
    path = Path(path)
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def open_private(path, mode="ab"):
    flags = os.O_WRONLY | os.O_CREAT
    if "a" in mode:
        flags |= os.O_APPEND
    else:
        flags |= os.O_TRUNC
    fd = os.open(path, flags, FILE_MODE)
    os.fchmod(fd, FILE_MODE)
    return os.fdopen(fd, mode)


def write_private_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(".%s.tmp.%d" % (path.name, os.getpid()))
    with open_private(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def session_dir(root, name):
    return Path(root) / "sessions" / name


def run_pointer(root, server_pid, pane_id):
    return Path(root) / "run" / str(server_pid) / pane_id.lstrip("%")
