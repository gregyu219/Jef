"""What a person can ask Jef to do or show, kept apart from any one way of
asking. The web app calls these, and so can other front ends (like the
Telegram link), so every one of them follows the same rules.

Nothing here makes an LLM or GitHub call except ``start_backlog_item``
(which clones the repo and starts a coordinator) and ``message_coordinator``
(which starts a coordinator turn).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jef.backlog import Backlog, BacklogItem, BacklogStatus
from jef.chats import Agent, Chats, ChatError, load_roster
from jef.pipeline import Phase
from jef.projects import (
    Project,
    StartError,
    auto_approve,
    gh_clone,
    goal_fields,
    known_repos,
    load_project,
    parse_repo,
    projects_on_repo,
    start_project,
    used_backlog_numbers,
)
from jef.status import Attention, needs_you, project_phase, read_status, write_status
from jef.tasks import TaskList, TaskStatus
from jef.usage import Usage, project_total
from jef.workspace import Workspace

NO_CLAUDE = "Claude Code isn't working yet, so no agent can start. See Settings."


class ActionError(RuntimeError):
    """A request Jef turned down before doing anything. ``reason`` is a short
    code a front end can map to its own wording; the message is plain text."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# --- Status ---------------------------------------------------------------


@dataclass
class ProjectSummary:
    slug: str
    name: str
    total: int
    counts: dict[TaskStatus, int]
    usage: Usage = Usage()
    # From status.md, so the home page needs no LLM call to show them.
    phase: str = ""
    coordinator: str = ""
    coordinator_status: str = ""
    problem: str = ""

    @property
    def merged(self) -> int:
        return self.counts[TaskStatus.MERGED]


def split_coordinator(value: str) -> tuple[str, str]:
    """"Katherine (Idle)" -> ("Katherine", "Idle"); "none" -> ("", "")."""
    value = value.strip()
    if not value or value == "none":
        return "", ""
    name, paren, rest = value.rpartition(" (")
    if paren and rest.endswith(")"):
        return name, rest[:-1]
    return value, ""


def summarize_projects(ws: Workspace) -> list[ProjectSummary]:
    summaries = []
    for slug in ws.project_slugs():
        tasks = TaskList.load(ws.project_dir(slug) / "tasks.md")
        status = read_status(ws, slug)
        coordinator, coordinator_status = split_coordinator(status.get("coordinator", ""))
        problem = status.get("problem", "")
        summaries.append(
            ProjectSummary(
                slug=slug,
                name=tasks.title or slug,
                total=len(tasks.tasks),
                counts=tasks.counts(),
                usage=project_total(ws, slug),
                phase=status.get("phase", ""),
                coordinator=coordinator,
                coordinator_status=coordinator_status,
                problem="" if problem == "none" else problem,
            )
        )
    return summaries


def refresh_statuses(ws: Workspace, roster: Callable[[str], list[Agent]] | None = None) -> int:
    """Rewrite every project's status.md from its files. ``roster`` gives a
    project's agents (Chats.refresh when Claude Code works); without it the
    saved agents.md is read. No LLM or GitHub call. Returns how many changed."""
    changed = 0
    for slug in ws.project_slugs():
        agents = roster(slug) if roster else load_roster(ws, slug)
        changed += write_status(ws, slug, agents)
    return changed


def backlog_projects(ws: Workspace) -> dict[int, str]:
    """Backlog item number -> the slug of the project started from it."""
    found = {}
    for slug in ws.project_slugs():
        number = goal_fields(ws, slug).get("backlog item", "").lstrip("#").strip()
        if number.isdigit():
            found[int(number)] = slug
    return found


def backlog_attention(ws: Workspace, backlog: Backlog) -> dict[int, tuple[str, Attention]]:
    """For each in-progress backlog item waiting on you: (its project's slug,
    what it needs). Read from the project files every time, so it's never
    stale and costs no LLM or GitHub call."""
    slugs = backlog_projects(ws)
    found = {}
    for item in backlog.items:
        slug = slugs.get(item.number)
        if item.status is not BacklogStatus.IN_PROGRESS or slug is None:
            continue
        attention = needs_you(ws, slug, load_roster(ws, slug))
        if attention is not None:
            found[item.number] = (slug, attention)
    return found


def backlog_auto_approve(ws: Workspace, backlog: Backlog) -> dict[int, tuple[bool, bool]]:
    """For each backlog item: (whether Auto approve is on, whether it can
    still be changed). A started item reads its project's goal.md, the
    source of truth; a Waiting one reads its backlog row. It stops being
    changeable once the item is done or its task list is already approved."""
    slugs = backlog_projects(ws)
    found = {}
    for item in backlog.items:
        slug = slugs.get(item.number)
        if slug is None:
            found[item.number] = (item.auto_approve, item.status is not BacklogStatus.DONE)
            continue
        phase = project_phase(ws, slug, load_roster(ws, slug))
        changeable = item.status is not BacklogStatus.DONE and phase not in (Phase.BUILDING, Phase.DONE, Phase.STOPPED)
        found[item.number] = (auto_approve(ws, slug), changeable)
    return found


# --- Coordinators ---------------------------------------------------------


def known_project(ws: Workspace, slug: str) -> Project | None:
    """The project with this slug, or None if there's no such (readable) one."""
    return load_project(ws, slug) if slug in ws.project_slugs() else None


def coordinators(ws: Workspace) -> dict[str, str]:
    """Coordinator name -> its project's slug."""
    found = {}
    for slug in ws.project_slugs():
        project = load_project(ws, slug)
        if project and project.coordinator:
            found[project.coordinator] = slug
    return found


def find_coordinator(ws: Workspace, name: str) -> Project | None:
    """The project whose coordinator is called ``name`` (any case), if any."""
    wanted = name.strip().casefold()
    for coordinator, slug in coordinators(ws).items():
        if coordinator.casefold() == wanted:
            return load_project(ws, slug)
    return None


def chat_error_reason(err: ChatError) -> str:
    """Why a coordinator turned a message down: "busy", "budget" or "stopped"."""
    said = str(err)
    return "busy" if "still working" in said else "budget" if "budget" in said else "stopped"


def message_coordinator(
    ws: Workspace, chats: Chats | None, slug: str, message: str, sender: str = "you"
) -> Project:
    """Send ``message`` to the project's coordinator, as ``sender``. Raises
    ActionError ("unknown", "empty", "claude") before sending, or the
    ChatError the coordinator's chat gave (see ``chat_error_reason``)."""
    project = known_project(ws, slug)
    if project is None:
        raise ActionError("unknown", f"There's no project {slug}.")
    text = message.strip()
    if not text:
        raise ActionError("empty", "Type a message first.")
    if chats is None:
        raise ActionError("claude", NO_CLAUDE)
    chats.send(slug, project.coordinator_agent, text, sender=sender)
    return project


# --- Backlog --------------------------------------------------------------


def add_backlog_item(ws: Workspace, feature: str) -> BacklogItem:
    """Add a feature to the backlog and save it. Its number is never one a
    deleted item's project still uses. Raises ValueError if it's empty."""
    backlog = Backlog.load(ws.backlog_file)
    item = backlog.add(feature, reserved=used_backlog_numbers(ws))
    backlog.save(ws.backlog_file)
    return item


def find_backlog_item(ws: Workspace, number: int) -> BacklogItem | None:
    return next((i for i in Backlog.load(ws.backlog_file).items if i.number == number), None)


@dataclass
class RepoChoice:
    """What the start form offers: the repo picked, the ones Jef already
    knows, and the projects already on the picked one (with their status)."""

    repo: str
    known: list[str]
    others: list


def repo_choice(ws: Workspace, repo: str = "") -> RepoChoice:
    """The repo to start with: ``repo`` if given, else the first known one."""
    repos = known_repos(ws)
    chosen = repo or (repos[0] if repos else "")
    try:
        others = projects_on_repo(ws, parse_repo(chosen)) if chosen else []
    except StartError:
        others = []
    return RepoChoice(chosen, repos, others)


def start_backlog_item(
    ws: Workspace,
    number: int,
    repo: str,
    chats: Chats | None,
    clone: Callable[[str, Path], None] = gh_clone,
    choose_name: Callable[[list[str]], str] = random.choice,
) -> Project:
    """Start a project from a Waiting backlog item on ``repo``. The caller
    must have had the person confirm first. Raises StartError."""
    if chats is None:
        raise StartError(NO_CLAUDE)
    return start_project(ws, number, repo, chats, clone=clone, choose_name=choose_name)
