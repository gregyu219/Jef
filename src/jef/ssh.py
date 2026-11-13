"""Make git over SSH give up on a dead connection.

A fetch or push whose connection dies (the Mac slept, the Wi-Fi dropped) can
otherwise wait forever: ssh doesn't notice, and the 5-minute timeouts Jef puts
on git don't count time asleep. With a keepalive, ssh notices within about a
minute and git fails, so Jef can report it and the coordinator can try again.
Agents inherit this too, so an implementer's own fetch can't hang either.
"""

from __future__ import annotations

import os
import subprocess

KEEPALIVE = "ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ConnectTimeout=30"


def keep_ssh_alive(environ: dict[str, str] | None = None, run=subprocess.run) -> bool:
    """Set GIT_SSH_COMMAND for Jef and its agents, unless you've already
    chosen an ssh command (GIT_SSH_COMMAND, GIT_SSH or core.sshCommand).
    Returns whether it set it."""
    env = os.environ if environ is None else environ
    if env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH"):
        return False
    try:
        result = run(
            ["git", "config", "--get", "core.sshCommand"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        if result.stdout.strip():
            return False
    except (OSError, subprocess.SubprocessError):
        pass
    env["GIT_SSH_COMMAND"] = KEEPALIVE
    return True
