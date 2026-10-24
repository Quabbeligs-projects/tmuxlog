"""Drive a real interactive bash/zsh through a pty with the tmuxlog hooks sourced."""

import os
import pty
import select
import signal
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INIT = REPO / "tmuxlog" / "shell" / "tmuxlog-init.sh"
PROMPT = "@@P@@ "
MARK = b"\x1b]6973;tmuxlog;"


class Shell:
    def __init__(self, shell: str, home: Path, env: dict | None = None, rc_pre: str = ""):
        self.shell = shell
        self.out = bytearray()
        e = {
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "TERM": "xterm",
            "LANG": "C.UTF-8",
        }
        e.update(env or {})
        rc = f"PS1='{PROMPT}'\nHISTFILE={home}/.hist\n{rc_pre}\nsource {INIT}\n"
        if shell == "bash":
            (home / ".bashrc").write_text(rc)
            e["INPUTRC"] = "/dev/null"
            argv = ["bash", "--noprofile", "--rcfile", str(home / ".bashrc"), "-i"]
        else:
            (home / ".zshenv").write_text("unsetopt GLOBAL_RCS\n")
            (home / ".zshrc").write_text(rc)
            e["ZDOTDIR"] = str(home)
            argv = ["zsh", "-i"]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(home)
            os.execvpe(argv[0], argv, e)
        self.wait_for(lambda: self.prompts() >= 1)

    def pump(self, seconds: float = 0.05) -> None:
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            r, _, _ = select.select([self.fd], [], [], max(left, 0))
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.out += data
            elif left <= 0:
                return

    def wait_for(self, pred, timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > end:
                raise TimeoutError(f"{self.shell}: timed out; output so far:\n{bytes(self.out)!r}")
            self.pump(0.05)

    def prompts(self) -> int:
        return self.out.count(PROMPT.encode())

    def send(self, text: str) -> None:
        os.write(self.fd, text.encode())

    def run(self, line: str, timeout: float = 10.0) -> None:
        """Type a line (\\n separates continuation lines), wait for the next prompt."""
        n = self.prompts()
        self.send(line.replace("\n", "\r") + "\r")
        self.wait_for(lambda: self.prompts() > n, timeout)

    def close(self) -> None:
        try:
            self.send("exit\r")
            end = time.monotonic() + 5
            while time.monotonic() < end:
                self.pump(0.05)
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    break
            else:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass
