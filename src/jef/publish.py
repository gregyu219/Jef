"""Jef pushes implementers' branches and opens their PRs itself.

Agents only commit. When an implementer reports ``STATUS: ready``, Jef:

1. checks it's on its own ``jef/...`` branch with nothing left uncommitted,
2. scans every line added in every commit on the branch (not just the
   final result, since the push sends all of them), the commit messages and
   the PR text for anything that looks like a secret, and refuses to push if
   it finds one,
3. pushes the branch (never ``main``: the target is always the agent's own
   ``jef/...`` branch), and
4. opens the PR with your GitHub login, or updates the one already open.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jef.secrets import SECRET_PATTERNS

BRANCH_PREFIX = "jef/"


class PublishError(RuntimeError):
    """Something the agent can fix; the message tells it what."""


def find_secrets_in_diff(diff: str) -> list[tuple[str, str]]:
    """(kind, file) for each added line that looks like a secret."""
    found: list[tuple[str, str]] = []
    current = "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            continue
        if not line.startswith("+"):
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(line) and (kind, current) not in found:
                found.append((kind, current))
                break
    return found


def find_secrets_in_text(text: str) -> list[str]:
    return [kind for kind, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


@dataclass
class Publisher:
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def _cmd(self, work: Path, *cmd: str, check: bool = True) -> str:
        result = self.run(list(cmd), cwd=work, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        if check and result.returncode != 0:
            said = " ".join((result.stderr or result.stdout).split())[:300]
            raise PublishError(f"`{' '.join(cmd[:3])}` failed: {said}")
        return result.stdout.strip()

    def publish(self, work: Path, branch: str, base: str, title: str, body: str) -> str:
        """Push the branch and open (or update) its PR. Returns the PR's URL."""
        if not branch.startswith(BRANCH_PREFIX) or branch in (base, "main", "master"):
            raise PublishError(f"Jef only pushes its own {BRANCH_PREFIX}… branches, not {branch!r}.")
        current = self._cmd(work, "git", "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            raise PublishError(f"You're on `{current}`, not your branch `{branch}`. Switch back with `git switch {branch}`.")
        if self._cmd(work, "git", "status", "--porcelain"):
            raise PublishError("There are uncommitted changes. Commit them (or remove them), then report ready again.")
        self._cmd(work, "git", "fetch", "--quiet", "origin", base)
        if self._cmd(work, "git", "rev-list", "--count", f"origin/{base}..HEAD") in ("", "0"):
            raise PublishError(f"There are no commits on `{branch}` yet. Commit your work, then report ready again.")

        # Every commit's own changes: a secret added in one commit and deleted
        # in a later one is still in the history the push sends to GitHub.
        history = self._cmd(work, "git", "log", "-p", "--format=", "--no-color", f"origin/{base}..HEAD")
        # And the final result, which also covers changes made while merging.
        diff = self._cmd(work, "git", "diff", "--no-color", f"origin/{base}...HEAD")
        messages = self._cmd(work, "git", "log", "--format=%B", f"origin/{base}..HEAD")
        found = find_secrets_in_diff(history)
        found += [hit for hit in find_secrets_in_diff(diff) if hit not in found]
        problems = [f"a {kind} in {path}" for kind, path in found]
        problems += [f"a {kind} in a commit message" for kind in find_secrets_in_text(messages)]
        problems += [f"a {kind} in the PR text" for kind in find_secrets_in_text(title + "\n" + body)]
        if problems:
            raise PublishError(
                "Jef didn't push: it found what looks like " + ", ".join(problems) + ". "
                "Remove it from the files and from the branch's history (amend or rebase the commits), "
                "then report ready again. If it's not really a secret, say so with STATUS: stuck."
            )

        # Always the agent's own branch, never main. Only Jef's agents use jef/ branches, so
        # --force-with-lease is safe and lets an agent rewrite its commits to drop a mistake.
        self._cmd(work, "git", "push", "--quiet", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}")
        existing = self._cmd(
            work, "gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url",
            check=False,
        )
        if existing.startswith("https://"):
            return existing  # the push updated the open PR
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(body)
            body_file = f.name
        try:
            out = self._cmd(
                work, "gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", body_file
            )
        finally:
            Path(body_file).unlink(missing_ok=True)
        url = next((w for w in out.split() if w.startswith("https://") and "/pull/" in w), "")
        if not url:
            raise PublishError(f"`gh pr create` didn't return a PR link: {out[:200]}")
        return url

    def comment(self, work: Path, url: str, text: str) -> None:
        """Leave a short note on a PR, like why its branch was rewritten."""
        if find_secrets_in_text(text):
            raise PublishError("The comment looks like it contains a secret.")
        self._cmd(work, "gh", "pr", "comment", url, "--body", text)
