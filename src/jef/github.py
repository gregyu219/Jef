"""Checking the GitHub login Jef uses.

For the MVP, Jef uses your own GitHub CLI (``gh``) login. Jef never reads or
stores the token itself; it only asks ``gh`` which account is signed in.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class GitHubStatus:
    installed: bool
    account: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.installed and self.account)


def github_status() -> GitHubStatus:
    if not shutil.which("gh"):
        return GitHubStatus(installed=False)
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return GitHubStatus(installed=True)
    account = result.stdout.strip() if result.returncode == 0 else ""
    return GitHubStatus(installed=True, account=account)
