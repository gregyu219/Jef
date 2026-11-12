"""The "Chat with Jef" on the home page.

Jef itself is a standing chat, like a project coordinator: one Claude Code
session you can ask about the backlog and how projects are going. Its first
message is a briefing on everything in the workspace, the way a new
coordinator's first message briefs it on its project.

The chat is kept outside ``projects/`` so it never shows up as a project:

    jef/projects/home/
      agents.md          the one "jef" agent
      chats/jef.jsonl    the conversation
      chats/jef-<time>.jsonl   earlier conversations, kept by Reset
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from jef import skills
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Agent, Chats, Role, load_roster, save_roster
from jef.projects import goal_fields, load_project
from jef.tasks import TaskList, TaskStatus
from jef.workspace import Workspace

HOME = "home"  # the chat's "project" key inside its own sub-workspace
NAME = "jef"


def jef_workspace(ws: Workspace) -> Workspace:
    return Workspace(ws.root / "jef")


def make_chats(ws: Workspace, claude_path: str) -> Chats:
    return Chats(jef_workspace(ws), claude_path)


def context_message(ws: Workspace) -> str:
    """The briefing Jef starts with: one line per backlog item and project."""
    lines = [
        "Hi Jef. You're the front desk of Jef, a software factory. Here is where things stand.",
        "",
        "## Backlog",
    ]
    items = Backlog.load(ws.backlog_file).items
    # Finished work is only counted, not listed: the briefing stays short as
    # projects pile up, and Jef can still read the files when asked.
    open_items = [i for i in items if i.status is not BacklogStatus.DONE]
    for item in open_items:
        who = f", coordinator {item.coordinator}" if item.coordinator else ""
        lines.append(f"- #{item.number} {item.feature} ({item.status.value}{who})")
    if len(open_items) < len(items):
        lines.append(f"- {len(items) - len(open_items)} done (see backlog.md)")
    if not items:
        lines.append("- (empty)")
    lines += ["", "## Projects"]
    slugs = ws.project_slugs()
    finished = [s for s in slugs if goal_fields(ws, s).get("done", "").lower() == "yes"]
    for slug in slugs:
        if slug in finished:
            continue
        project = load_project(ws, slug)
        tasks = TaskList.load(ws.project_dir(slug) / "tasks.md")
        merged = tasks.counts()[TaskStatus.MERGED]
        name = project.name if project else (tasks.title or slug)
        repo = project.repo if project and project.repo else "no repo"
        coordinator = project.coordinator if project and project.coordinator else "none"
        lines.append(
            f"- {name} (folder projects/{slug}, repo {repo}, coordinator {coordinator}, "
            f"{merged} of {len(tasks.tasks)} tasks merged)"
        )
    if finished:
        lines.append(f"- {len(finished)} finished (folders: {', '.join(finished)})")
    if not slugs:
        lines.append("- (none yet)")
    lines += [
        "",
        f"Your working folder is Jef's workspace, {ws.root}. For details, read backlog.md "
        "and projects/<folder>/goal.md, tasks.md and research.md.",
    ]
    return "\n".join(lines)


def agent(chats: Chats) -> Agent | None:
    return next((a for a in chats.refresh(HOME) if a.name == NAME), None)


def send(chats: Chats, ws: Workspace, message: str) -> Agent:
    """Message Jef. The first message starts the session with the briefing."""
    with chats.lock:
        if agent(chats) is None:
            ws.root.mkdir(parents=True, exist_ok=True)
            return chats.spawn(
                HOME,
                NAME,
                Role.JEF,
                context_message(ws) + "\n\nThe person says: " + message,
                ws.root,
                instructions=skills.load("jef"),
            )
        return chats.send(HOME, NAME, message)


def reset(chats: Chats, ws: Workspace, now: Callable[[], datetime] = datetime.now) -> Agent:
    """Start Jef over: end the session, keep the old conversation as
    ``chats/jef-<timestamp>.jsonl``, and start a new session with a fresh
    briefing, so Jef greets you already knowing where things stand."""
    with chats.lock:
        if agent(chats) is not None:
            chats.stop(HOME, NAME)
        log = chats.chat_file(HOME, NAME)
        if log.exists():
            stamp = now().strftime("%Y%m%d-%H%M%S")
            archive = log.with_name(f"{NAME}-{stamp}.jsonl")
            n = 2
            while archive.exists():
                archive = log.with_name(f"{NAME}-{stamp}-{n}.jsonl")
                n += 1
            log.rename(archive)
        save_roster(chats.workspace, HOME, [a for a in load_roster(chats.workspace, HOME) if a.name != NAME])
        ws.root.mkdir(parents=True, exist_ok=True)
        return chats.spawn(HOME, NAME, Role.JEF, context_message(ws), ws.root, instructions=skills.load("jef"))
