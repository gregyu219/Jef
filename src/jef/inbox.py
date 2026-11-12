"""Jef's inbox: requests from coordinators.

A coordinator runs its project with its own ``jef`` commands, like
``jef task start 1`` (see ``jef.coordinate``). Those commands don't change anything themselves: they
drop a request in the project folder, and the running Jef carries it out
within a few seconds and writes back the answer. That way Jef is the only
thing that ever writes the project's files, starts agents or pushes code, so
its rules (one task at a time, the secret check, never pushing ``main``) hold
whatever a coordinator asks.

    projects/<slug>/requests/<id>.json         waiting for Jef
    projects/<slug>/requests/<id>.answer.json  Jef's answer, until it's read
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from jef.workspace import Workspace

REQUESTS_DIR = "requests"
ALIVE_FILE = "jef.alive"
ALIVE_SECONDS = 30  # Jef touches the alive file every few seconds while it runs


@dataclass
class Request:
    id: str
    sender: str  # the agent name the command ran as, like "ada"
    command: str
    args: dict

    @property
    def summary(self) -> str:
        shown = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in self.args.items())
        return f"{self.command}({shown})"


def _folder(ws: Workspace, slug: str) -> Path:
    return ws.project_dir(slug) / REQUESTS_DIR


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def submit(ws: Workspace, slug: str, sender: str, command: str, args: dict) -> str:
    """Queue a request. Returns its id."""
    request_id = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    _write_atomic(
        _folder(ws, slug) / f"{request_id}.json",
        {"id": request_id, "sender": sender, "command": command, "args": args},
    )
    return request_id


def pending(ws: Workspace, slug: str) -> list[Request]:
    """Waiting requests, oldest first."""
    folder = _folder(ws, slug)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.json")):
        if path.name.endswith(".answer.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found.append(Request(str(data["id"]), str(data["sender"]), str(data["command"]), dict(data["args"])))
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)  # half-written or hand-made nonsense
    return found


def answer(ws: Workspace, slug: str, request_id: str, ok: bool, message: str) -> None:
    folder = _folder(ws, slug)
    _write_atomic(folder / f"{request_id}.answer.json", {"ok": ok, "message": message})
    (folder / f"{request_id}.json").unlink(missing_ok=True)


def wait_for_answer(ws: Workspace, slug: str, request_id: str, seconds: float, poll: float = 0.5) -> dict | None:
    """Jef's answer, or None if it didn't come in time."""
    path = _folder(ws, slug) / f"{request_id}.answer.json"
    deadline = time.monotonic() + seconds
    while True:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if data is not None:
                path.unlink(missing_ok=True)
                return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def mark_alive(ws: Workspace) -> None:
    path = ws.root / ALIVE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def jef_is_running(ws: Workspace, now: float | None = None) -> bool:
    path = ws.root / ALIVE_FILE
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age < ALIVE_SECONDS
