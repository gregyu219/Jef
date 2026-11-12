"""Keeping your keys safe.

Jef stores secrets (like your Anthropic API key) in your operating system's
keychain, never in files under a project and never in anything that gets
committed or pushed. On a Mac that is the Keychain app, under the service
name "jef".

``find_leaks`` is the last line of defence: Jef runs it on anything it is
about to commit or publish to GitHub, and refuses to continue if it finds a
known secret or something that looks like one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

SERVICE = "jef"
ANTHROPIC_API_KEY = "anthropic_api_key"

# Shapes of common secrets, so we catch ones Jef didn't store itself.
SECRET_PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


class Backend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def _keyring_backend() -> Backend:
    import keyring

    return keyring


class SecretStore:
    def __init__(self, backend: Backend | None = None):
        self._backend = backend or _keyring_backend()

    def get(self, name: str) -> str | None:
        return self._backend.get_password(SERVICE, name) or None

    def set(self, name: str, value: str) -> None:
        value = value.strip()
        if not value:
            raise ValueError("A key can't be empty.")
        self._backend.set_password(SERVICE, name, value)

    def delete(self, name: str) -> None:
        try:
            self._backend.delete_password(SERVICE, name)
        except Exception:  # Already gone is fine.
            pass

    def known_values(self) -> list[str]:
        value = self.get(ANTHROPIC_API_KEY)
        return [value] if value else []


def mask(value: str | None) -> str:
    """Show just enough of a key to recognise it, e.g. 'sk-ant-…3f9a'."""
    if not value:
        return ""
    if len(value) <= 12:
        return "…" + value[-2:]
    return f"{value[:7]}…{value[-4:]}"


@dataclass
class Leak:
    kind: str
    line: int


def find_leaks(text: str, known_secrets: list[str] | None = None) -> list[Leak]:
    """Find secrets in text. Reports where, never the secret itself."""
    leaks: list[Leak] = []
    known = [s for s in (known_secrets or []) if len(s) >= 8]
    for number, line in enumerate(text.splitlines(), start=1):
        if any(secret in line for secret in known):
            leaks.append(Leak("Saved Jef secret", number))
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                leaks.append(Leak(kind, number))
                break
    return leaks
