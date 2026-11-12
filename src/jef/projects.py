"""Turning a backlog item into a project.

Nothing starts without you: this only runs after you press Start, give the
GitHub repo and confirm. Starting a project:

1. creates ``projects/<slug>/`` with ``goal.md`` and an empty ``tasks.md``,
2. clones the repo into ``projects/<slug>/repo`` with your ``gh`` login,
3. spawns a named coordinator chat in that clone, and
4. marks the backlog item *In progress* with the coordinator's name.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from jef import skills
from jef.backlog import Backlog, BacklogItem, BacklogStatus
from jef.chats import Chats, ChatError, Role
from jef.tasks import TaskList
from jef.workspace import Workspace

# Coordinators get a person's name, so you can talk to them by name.
COORDINATOR_NAMES = [
    "Ada", "Grace", "Linus", "Barbara", "Ken", "Margaret", "Dennis", "Frances",
    "Alan", "Radia", "Guido", "Hedy", "Edsger", "Katherine", "Donald", "Lynn",
]

REPO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
URL_PREFIXES = ("https://github.com/", "http://github.com/", "github.com/", "git@github.com:")


class StartError(RuntimeError):
    pass


def parse_repo(text: str) -> str:
    """Accept "owner/name" or a GitHub URL; return "owner/name"."""
    repo = text.strip()
    for prefix in URL_PREFIXES:
        if repo.lower().startswith(prefix):
            repo = repo[len(prefix):]
            break
    repo = repo.removesuffix("/").removesuffix(".git")
    if not REPO_PATTERN.match(repo) or repo.split("/")[1] in (".", ".."):
        raise StartError("Give the repo as owner/name, like gregyu219/Jef, or paste its GitHub link.")
    return repo


def slugify(text: str, taken: set[str]) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    base = "-".join(words)[:40].strip("-") or "project"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def pick_coordinator_name(backlog: Backlog) -> str:
    used = {i.coordinator for i in backlog.items}
    for name in COORDINATOR_NAMES:
        if name not in used:
            return name
    return f"Coordinator {len(used) + 1}"


def agent_name(display: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")


def gh_clone(repo: str, dest: Path) -> None:
    result = subprocess.run(
        ["gh", "repo", "clone", repo, str(dest), "--", "--quiet"],
        capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        said = " ".join((result.stderr or result.stdout).split())[:300]
        raise StartError(f"Couldn't clone {repo}: {said or 'gh failed'}")


@dataclass
class Project:
    slug: str
    name: str
    repo: str
    coordinator: str  # the display name, like "Ada"

    @property
    def coordinator_agent(self) -> str:
        return agent_name(self.coordinator)


def goal_text(item: BacklogItem, repo: str, coordinator: str) -> str:
    return (
        f"# Goal: {item.feature}\n\n"
        f"- Repo: {repo}\n"
        f"- Coordinator: {coordinator}\n"
        f"- Backlog item: #{item.number}\n"
        f"- Started: {date.today().isoformat()}\n\n"
        "## What you asked for\n\n"
        f"{item.feature}\n"
    )


def load_project(ws: Workspace, slug: str) -> Project | None:
    goal = ws.project_dir(slug) / "goal.md"
    if not goal.exists():
        return None
    name, fields = slug, {}
    for line in goal.read_text(encoding="utf-8").splitlines():
        if line.startswith("# Goal:"):
            name = line[len("# Goal:"):].strip()
        elif line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            fields[key.strip().lower()] = value.strip()
    return Project(slug, name, fields.get("repo", ""), fields.get("coordinator", ""))


def first_message(item: BacklogItem, repo: str, coordinator: str) -> str:
    return (
        f"Hi {coordinator}. You're the coordinator for a new project.\n\n"
        f"The request: {item.feature}\n"
        f"The repo: {repo} (your working folder is a fresh clone of it)\n\n"
        "Follow your coordinator instructions: look around the repo, then reply with "
        "what you understand, what in the repo matters, and any questions."
    )


def start_project(
    ws: Workspace,
    number: int,
    repo_text: str,
    chats: Chats,
    clone: Callable[[str, Path], None] = gh_clone,
) -> Project:
    backlog = Backlog.load(ws.backlog_file)
    item = next((i for i in backlog.items if i.number == number), None)
    if item is None:
        raise StartError(f"There's no backlog item #{number}.")
    if item.status is not BacklogStatus.WAITING:
        raise StartError(f"Item #{number} has already been started.")
    repo = parse_repo(repo_text)

    slug = slugify(item.feature, set(ws.project_slugs()))
    coordinator = pick_coordinator_name(backlog)
    folder = ws.project_dir(slug)
    folder.mkdir(parents=True)
    try:
        (folder / "goal.md").write_text(goal_text(item, repo, coordinator), encoding="utf-8")
        TaskList(title=item.feature).save(folder / "tasks.md")
        clone(repo, folder / "repo")
        chats.spawn(
            slug,
            agent_name(coordinator),
            Role.COORDINATOR,
            first_message(item, repo, coordinator),
            folder / "repo",
            instructions=skills.load("coordinator"),
        )
    except (StartError, ChatError, OSError, subprocess.SubprocessError) as err:
        shutil.rmtree(folder, ignore_errors=True)  # leave nothing half-started
        raise err if isinstance(err, StartError) else StartError(str(err)) from err

    item.status = BacklogStatus.IN_PROGRESS
    item.coordinator = coordinator
    backlog.save(ws.backlog_file)
    return Project(slug, item.feature, repo, coordinator)
