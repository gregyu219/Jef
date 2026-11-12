"""Catching secrets before they reach GitHub.

Jef doesn't store any keys itself: Claude Code and the GitHub CLI each keep
their own login. But agents commit and push with your GitHub login, so
``find_leaks`` looks for things that look like secrets (API keys, GitHub
tokens, private keys) in text that's about to be published. It reports where
it found them, never the secret itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECRET_PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


@dataclass
class Leak:
    kind: str
    line: int


def find_leaks(text: str, known_secrets: list[str] | None = None) -> list[Leak]:
    """Find secrets in text. ``known_secrets`` are exact values to look for too."""
    leaks: list[Leak] = []
    known = [s for s in (known_secrets or []) if len(s) >= 8]
    for number, line in enumerate(text.splitlines(), start=1):
        if any(secret in line for secret in known):
            leaks.append(Leak("Known secret", number))
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                leaks.append(Leak(kind, number))
                break
    return leaks
