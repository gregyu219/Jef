"""Talking to Claude Code.

Jef doesn't hold an Anthropic key. It runs the ``claude`` command (Claude
Code) in headless mode, ``claude -p``, and Claude Code signs in however you
set it up: your Claude login or your own API key. That's the KiroCrew
approach: the agent program owns the login, Jef only drives it.

``check()`` makes one tiny call so Jef knows, before it starts, that agents
will actually be able to run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable

CHECK_PROMPT = "Reply with only the word: ok"
# The check uses the smallest model so it costs next to nothing.
CHECK_MODEL = "haiku"
CHECK_TIMEOUT = 120

INSTALL_HELP = (
    "Jef runs its agents with Claude Code, and the `claude` command isn't installed.\n"
    "Install it (https://code.claude.com/docs/en/quickstart), run `claude` once and sign in,\n"
    "then press Check again in Jef's Settings."
)
SIGN_IN_HELP = (
    "Run `claude` in a terminal and sign in (type /login if it doesn't ask),\n"
    "then press Check again in Jef's Settings."
)

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class ClaudeCheck:
    ok: bool
    problem: str = ""  # "", "missing", "failed" or "timeout"
    detail: str = ""  # what Claude Code said, shortened
    path: str = ""  # where the claude command lives

    @property
    def help(self) -> str:
        if self.ok:
            return ""
        if self.problem == "missing":
            return INSTALL_HELP
        said = f"Claude Code didn't answer: {self.detail}\n" if self.detail else "Claude Code didn't answer.\n"
        if self.problem == "timeout":
            said = "Claude Code didn't answer within two minutes.\n"
        return said + SIGN_IN_HELP


def _shorten(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def find_claude(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """The full path to ``claude``, so it still works where PATH is shorter (like tmux)."""
    found = which("claude")
    if found:
        return found
    # The native installer puts it here, which may not be on PATH yet.
    fallback = os.path.expanduser("~/.local/bin/claude")
    return fallback if os.access(fallback, os.X_OK) else None


def check(
    runner: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    timeout: float = CHECK_TIMEOUT,
) -> ClaudeCheck:
    """Make one tiny headless call and report whether it worked."""
    path = find_claude(which)
    if not path:
        return ClaudeCheck(False, "missing")
    cmd = [
        path, "-p", CHECK_PROMPT,
        "--output-format", "json",
        "--model", CHECK_MODEL,
        "--max-turns", "1",
        "--no-session-persistence",
    ]
    # Run in an empty folder so no project's settings or hooks get involved.
    with tempfile.TemporaryDirectory(prefix="jef-check-") as empty:
        try:
            result = runner(
                cmd, cwd=empty, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired:
            return ClaudeCheck(False, "timeout", path=path)
        except OSError as err:
            return ClaudeCheck(False, "failed", _shorten(str(err)), path)
    try:
        reply = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return ClaudeCheck(False, "failed", _shorten(result.stderr or result.stdout or "no output"), path)
    if result.returncode != 0 or reply.get("is_error"):
        return ClaudeCheck(False, "failed", _shorten(str(reply.get("result") or result.stderr)), path)
    return ClaudeCheck(True, path=path)
