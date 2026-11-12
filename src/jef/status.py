"""A project's status at a glance, in a plain file anyone can read.

Jef works out where a project is from its own files (goal.md, tasks.md and
agents.md), with no LLM call. This module puts that together in one place
and writes it to ``projects/<slug>/status.md``:

    # Status: Show the version in the footer

    - Phase: Building
    - Coordinator: Ada (Idle)
    - Coordinator note: Approved the tasks
    - Problem: none
    - Tasks: 1 of 2 merged
    - Updated: 2026-11-11 14:05

    | # | Task | Stage | Agent | Agent status | PR | Note |
    | --- | --- | --- | --- | --- | --- | --- |
    | 1 | Show it | Merged | impl-1 | Stopped | https://… | … |

The heartbeat rewrites it whenever something in it changes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from jef import mdtable
from jef.chats import Agent, Status
from jef.projects import goal_fields, load_project
from jef.tasks import TaskList, TaskStatus
from jef.workspace import Workspace

STATUS_FILE = "status.md"
HEADERS = ["#", "Task", "Stage", "Agent", "Agent status", "PR", "Note"]
UPDATED = "- Updated:"
NOTE_LIMIT = 160


class Phase(str, Enum):
    UNDERSTANDING = "Understanding"
    RESEARCH = "Research"
    PLANNING = "Planning"
    APPROVAL = "Waiting for your approval"
    BUILDING = "Building"
    DONE = "Done"
    STOPPED = "Stopped"


def _one_line(text: str, limit: int = NOTE_LIMIT) -> str:
    """Agent notes are untrusted text: keep them short and on one line."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_path(ws: Workspace, slug: str) -> Path:
    return ws.project_dir(slug) / STATUS_FILE


def project_phase(ws: Workspace, slug: str, agents: list[Agent]) -> Phase:
    """The project's phase, from its files and the roster ``agents``."""
    fields = goal_fields(ws, slug)
    if fields.get("done", "").lower() == "yes":
        return Phase.DONE
    project = load_project(ws, slug)
    coordinator = next((a for a in agents if a.name == project.coordinator_agent), None) if project else None
    if coordinator is not None and coordinator.status is Status.STOPPED:
        return Phase.STOPPED
    if fields.get("tasks approved", "").lower() == "yes":
        return Phase.BUILDING
    if TaskList.load(ws.project_dir(slug) / "tasks.md").tasks:
        return Phase.APPROVAL
    research = fields.get("research", "").lower()
    if research == "done":
        return Phase.PLANNING
    if research == "started":
        return Phase.RESEARCH
    return Phase.UNDERSTANDING


def render_status(ws: Workspace, slug: str, agents: list[Agent], now: datetime | None = None) -> str:
    project = load_project(ws, slug)
    fields = goal_fields(ws, slug)
    tasks = TaskList.load(ws.project_dir(slug) / "tasks.md").tasks
    by_name = {a.name: a for a in agents}

    name = project.name if project else slug
    coordinator = by_name.get(project.coordinator_agent) if project and project.coordinator else None
    if project and project.coordinator:
        who = project.coordinator + (f" ({coordinator.status.value})" if coordinator else " (not started)")
    else:
        who = "none"
    problem = fields.get("problem", "") or "none"
    merged = sum(t.status is TaskStatus.MERGED for t in tasks)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")

    doc = mdtable.TableDoc(
        before=[
            f"# Status: {_one_line(name, 200)}",
            "",
            f"- Phase: {project_phase(ws, slug, agents).value}",
            f"- Coordinator: {who}",
            f"- Coordinator note: {_one_line(coordinator.note) if coordinator and coordinator.note else 'none'}",
            f"- Problem: {_one_line(problem, 300)}",
            f"- Tasks: {merged} of {len(tasks)} merged" if tasks else "- Tasks: none yet",
            f"{UPDATED} {stamp}",
            "",
        ]
    )
    if tasks:
        doc.headers = list(HEADERS)
        for t in tasks:
            agent = by_name.get(t.agent) if t.agent else None
            doc.rows.append([
                str(t.number),
                t.title,
                t.status.value,
                t.agent,
                agent.status.value if agent else "",
                t.pr,
                _one_line(agent.note) if agent else "",
            ])
    else:
        doc.before.append("No tasks yet.")
    return mdtable.render(doc)


def _without_updated(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith(UPDATED)]


def write_status(ws: Workspace, slug: str, agents: list[Agent], now: datetime | None = None) -> bool:
    """Write status.md if anything but its Updated line changed. Returns
    whether it wrote, so the heartbeat doesn't rewrite it every tick."""
    if not ws.project_dir(slug).is_dir():
        return False
    text = render_status(ws, slug, agents, now)
    path = status_path(ws, slug)
    if path.exists() and _without_updated(path.read_text(encoding="utf-8")) == _without_updated(text):
        return False
    path.write_text(text, encoding="utf-8")
    return True


def read_status(ws: Workspace, slug: str) -> dict[str, str]:
    """The "- Key: value" lines of status.md, keys lowercased; {} if there's none."""
    path = status_path(ws, slug)
    fields: dict[str, str] = {}
    if not path.exists():
        return fields
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            break  # the task table
        if line.startswith("# Status:"):
            fields["name"] = line[len("# Status:"):].strip()
        elif line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            fields[key.strip().lower()] = value.strip()
    return fields
