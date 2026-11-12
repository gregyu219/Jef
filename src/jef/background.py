"""Running Jef in the background with tmux.

``jef start`` runs ``jef run`` inside a detached tmux session named "jef",
so it keeps going after you close the terminal. On a Mac it is wrapped in
``caffeinate -is`` so the computer doesn't fall asleep while Jef works.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

SESSION = "jef"
PORT_OPTION = "@jef_port"
# "=jef:" means exactly the session named jef. Plain "=jef" fails for set-option and show-options.
TARGET = f"={SESSION}:"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


class TmuxMissing(RuntimeError):
    pass


@dataclass
class Tmux:
    """A thin wrapper around the tmux command, swappable in tests."""

    runner: Runner = _run
    which: Callable[[str], str | None] = shutil.which
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))

    def require(self) -> None:
        if not self.which("tmux"):
            raise TmuxMissing(
                "jef start needs tmux to keep Jef running in the background.\n"
                "Install it with:  brew install tmux\n"
                "Or run Jef in this terminal instead:  jef run"
            )

    def session_exists(self) -> bool:
        """Is there any tmux session named "jef", whoever started it?"""
        if not self.which("tmux"):
            return False
        return self.runner(["tmux", "has-session", "-t", TARGET]).returncode == 0

    def is_running(self) -> bool:
        """Is Jef's own session running? Jef marks its session with its port."""
        return self.session_exists() and self.port() is not None

    def foreign_session(self) -> bool:
        """A session named "jef" that Jef didn't start. Jef never touches it."""
        return self.session_exists() and self.port() is None

    def port(self) -> int | None:
        result = self.runner(["tmux", "show-options", "-t", TARGET, "-v", PORT_OPTION])
        value = result.stdout.strip()
        return int(value) if result.returncode == 0 and value.isdigit() else None

    def start_command(self, port: int) -> list[str]:
        """The tmux command that starts Jef in a new background session."""
        cmd = ["tmux", "new-session", "-d", "-s", SESSION]
        # A running tmux server keeps its own environment, so pass JEF_HOME along explicitly.
        if "JEF_HOME" in self.env:
            cmd += ["-e", f"JEF_HOME={self.env['JEF_HOME']}"]
        inner = [sys.executable, "-m", "jef", "run", "--no-browser", "--claude-checked", "--port", str(port)]
        if self.which("caffeinate"):
            inner = ["caffeinate", "-is", *inner]
        return cmd + ["--", *inner]

    def start(self, port: int) -> None:
        self.require()
        result = self.runner(self.start_command(port))
        if result.returncode != 0:
            raise RuntimeError(f"tmux couldn't start Jef: {result.stderr.strip()}")
        self.runner(["tmux", "set-option", "-t", TARGET, PORT_OPTION, str(port)])

    def stop(self) -> None:
        self.runner(["tmux", "kill-session", "-t", TARGET])
