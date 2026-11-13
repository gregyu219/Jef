"""What a running Jef shares: the Claude Code check, the agents' chats, Jef's
own chat and the pipeline.

The web app and Jef's background threads (the heartbeat, and later the
Telegram link) all take these from one ``Runtime``. That matters because a
``Chats`` remembers the turns it started in memory: a second ``Chats`` for the
same workspace would think those turns had ended.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol

from jef import jefchat, models
from jef.chats import Chats
from jef.claude import ClaudeCheck
from jef.pipeline import Pipeline
from jef.usage import budget_left
from jef.workspace import Workspace

log = logging.getLogger(__name__)


class Stoppable(Protocol):
    def stop(self) -> None: ...


class Runtime:
    """Builds the chats and pipeline once Claude Code works, and hands every
    caller the same ones. Safe to use from several threads."""

    def __init__(
        self,
        workspace: Workspace,
        claude_status: ClaudeCheck | None = None,
        chats: Chats | None = None,
        jef_chats: Chats | None = None,
        pipeline: Pipeline | None = None,
    ):
        self.workspace = workspace
        self.claude = claude_status  # the latest Claude Code check, if any
        self.chats = chats
        self.jef_chats = jef_chats
        self.pipeline = pipeline
        self._lock = threading.RLock()
        self._ready: list[Callable[[Pipeline], None]] = []
        self._threads: list[Stoppable] = []

    def get_chats(self) -> Chats | None:
        """Agents can only run once Claude Code has been checked and works."""
        with self._lock:
            status = self.claude
            if self.chats is None and status is not None and status.ok:
                ws = self.workspace
                self.chats = Chats(
                    ws,
                    status.path,
                    budget_left=lambda slug: budget_left(ws, slug),
                    model_for=lambda slug, role: models.model_for(ws, slug, role),
                )
            return self.chats

    def get_jef_chats(self) -> Chats | None:
        """The home page's own chat, kept apart from the projects."""
        with self._lock:
            status = self.claude
            if self.jef_chats is None and status is not None and status.ok:
                self.jef_chats = jefchat.make_chats(self.workspace, status.path)
            return self.jef_chats

    def get_pipeline(self) -> Pipeline | None:
        """Projects can only run once Claude Code works. The first time it
        does (at start, or after Check again), the ``on_agents_ready``
        callbacks run, once."""
        with self._lock:
            runner = self.get_chats()
            created = self.pipeline is None and runner is not None
            if created:
                self.pipeline = Pipeline(self.workspace, runner)
            pipeline, callbacks = self.pipeline, list(self._ready)
        if created:
            # Outside the lock: a callback (the heartbeat's first tick) may be
            # slow, and other threads still need the chats meanwhile.
            for callback in callbacks:
                callback(pipeline)
        return pipeline

    def on_agents_ready(self, callback: Callable[[Pipeline], None]) -> None:
        """Run ``callback`` once with the pipeline, when agents can first run."""
        with self._lock:
            self._ready.append(callback)

    def keep(self, thread: Stoppable) -> None:
        """Remember a background thread, so ``stop`` ends it."""
        with self._lock:
            self._threads.append(thread)

    def stop(self) -> None:
        """When Jef shuts down: stop the background threads, then end the
        turns still running."""
        with self._lock:
            threads, self._threads = list(self._threads), []
            runners = [r for r in (self.chats, self.jef_chats) if r is not None]
        for thread in threads:
            try:
                thread.stop()
            except Exception:
                log.exception("Couldn't stop %r", thread)
        for runner in runners:
            runner.stop_running_turns()
