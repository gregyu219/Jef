"""Turning a backlog item into a project.

Nothing starts without you: this only runs after you press Start, give the
GitHub repo and confirm. Starting a project:

1. creates ``projects/<slug>/`` with ``goal.md`` and an empty ``tasks.md``,
2. clones the repo into ``projects/<slug>/repo`` with your ``gh`` login,
3. spawns a named coordinator chat in that clone,
4. writes a first ``status.md`` (see ``jef.status``), and
5. marks the backlog item *In progress* with the coordinator's name.
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from jef import skills
from jef.backlog import Backlog, BacklogItem, BacklogStatus
from jef.chats import Chats, ChatError, Role, load_roster
from jef.tasks import TaskList
from jef.workspace import Workspace

# Coordinators get a person's name, so you can talk to them by name.
# Each new project gets one at random from those not already in use.
COORDINATOR_NAMES = [
    "Taryn", "Miucci", "Joseph", "Serena", "Jerry", "Billy", "David",
    "Ada", "Grace", "Linus", "Barbara", "Ken", "Margaret", "Dennis", "Frances",
    "Alan", "Radia", "Guido", "Hedy", "Edsger", "Katherine", "Donald", "Lynn",
]

DEFAULT_HEARTBEAT_MINUTES = 60
# How many tasks a project may build side by side. One by default, since tasks
# often build on each other; the person can raise it per project.
DEFAULT_IMPLEMENTERS_AT_ONCE = 1
MAX_IMPLEMENTERS_AT_ONCE = 5

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


# Little words dropped from folder names, so "Show the version in the footer"
# becomes "show-version-footer".
STOPWORDS = {
    "a", "an", "the", "in", "on", "of", "to", "for", "and", "or", "with", "into",
    "at", "by", "from", "as", "is", "be", "it", "its", "my", "our", "this", "that",
}
MAX_SLUG_WORDS = 5
MAX_SLUG_LENGTH = 40


def slugify(text: str, taken: set[str]) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    words = [w for w in words if w not in STOPWORDS] or words
    kept: list[str] = []
    for word in words[:MAX_SLUG_WORDS]:
        # Whole words only: stop before one would make the name too long.
        if kept and len("-".join(kept + [word])) > MAX_SLUG_LENGTH:
            break
        kept.append(word[:MAX_SLUG_LENGTH])
    base = "-".join(kept) or "project"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def pick_coordinator_name(
    backlog: Backlog,
    choose: Callable[[list[str]], str] = random.choice,
    taken: Iterable[str] = (),
) -> str:
    """A name no project has used. ``taken`` holds names from project folders,
    so a project whose backlog row was deleted keeps its name to itself."""
    used = {i.coordinator for i in backlog.items if i.coordinator} | {n for n in taken if n}
    free = [name for name in COORDINATOR_NAMES if name not in used]
    if free:
        return choose(free)
    n = len(used) + 1
    while f"Coordinator {n}" in used:
        n += 1
    return f"Coordinator {n}"


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
        f"- Started: {date.today().isoformat()}\n"
        f"- Heartbeat: {DEFAULT_HEARTBEAT_MINUTES} minutes\n"
        f"- Auto approve: {'yes' if item.auto_approve else 'no'}\n\n"
        "## What you asked for\n\n"
        f"{item.feature}\n"
    )


def goal_fields(ws: Workspace, slug: str) -> dict[str, str]:
    """The "- Key: value" lines at the top of goal.md, keys lowercased."""
    goal = ws.project_dir(slug) / "goal.md"
    fields: dict[str, str] = {}
    if not goal.exists():
        return fields
    for line in goal.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            break  # only the list under the title
        if line.startswith("# Goal:"):
            fields["name"] = line[len("# Goal:"):].strip()
        elif line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            fields[key.strip().lower()] = value.strip()
    return fields


def implementers_at_once(ws: Workspace, slug: str) -> int:
    """How many tasks the project may build at once, from goal.md's
    ``Implementers at once`` field, or the default if it's missing or odd."""
    value = goal_fields(ws, slug).get("implementers at once", "")
    if value.isdigit() and 1 <= int(value) <= MAX_IMPLEMENTERS_AT_ONCE:
        return int(value)
    return DEFAULT_IMPLEMENTERS_AT_ONCE


def auto_approve(ws: Workspace, slug: str) -> bool:
    """Whether Jef's heartbeat approves the project's task list by itself,
    from goal.md's ``Auto approve`` field. Off unless it says ``yes``."""
    return goal_fields(ws, slug).get("auto approve", "").lower() == "yes"


def set_goal_field(ws: Workspace, slug: str, key: str, value: str) -> None:
    """Add or change one "- Key: value" line at the top of goal.md."""
    goal = ws.project_dir(slug) / "goal.md"
    lines = goal.read_text(encoding="utf-8").splitlines()
    new = f"- {key}: {value}"
    last_field = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            break
        if line.startswith("- ") and ":" in line:
            last_field = i
            if line[2:].split(":", 1)[0].strip().lower() == key.lower():
                lines[i] = new
                goal.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return
    lines.insert((last_field + 1) if last_field is not None else 1, new)
    goal.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_project(ws: Workspace, slug: str) -> Project | None:
    fields = goal_fields(ws, slug)
    if not fields:
        return None
    return Project(slug, fields.get("name", slug), fields.get("repo", ""), fields.get("coordinator", ""))


def project_for_backlog_item(ws: Workspace, number: int) -> str | None:
    """The slug of the project started from backlog item ``number``, if any."""
    for slug in ws.project_slugs():
        if goal_fields(ws, slug).get("backlog item", "").lstrip("#").strip() == str(number):
            return slug
    return None


class DeleteError(RuntimeError):
    pass


def delete_project(ws: Workspace, slug: str, chats: Chats | None = None) -> None:
    """Delete a project, whatever state it is in: stop its agents' running
    turns, delete its folder (clone, worktrees, chats, goal, tasks, status)
    and remove its backlog row. Its branches and PRs on GitHub stay."""
    if slug not in ws.project_slugs():
        raise DeleteError(f"There's no project {slug!r}.")
    folder = ws.project_dir(slug)
    # Only ever a real folder directly inside projects/, never a link out of it.
    if folder.is_symlink() or folder.resolve().parent != ws.projects_dir.resolve():
        raise DeleteError(f"{slug!r} isn't a project folder Jef can delete.")
    # Under the lock, so the heartbeat can't start a turn in it meanwhile.
    with chats.lock if chats is not None else nullcontext():
        if chats is not None:
            chats.stop_project(slug)
        number = goal_fields(ws, slug).get("backlog item", "").lstrip("#").strip()
        try:
            shutil.rmtree(folder)
        except OSError as err:
            raise DeleteError(f"Couldn't delete {folder}: {err}") from err
        backlog = Backlog.load(ws.backlog_file)
        kept = [i for i in backlog.items if str(i.number) != number]
        if len(kept) != len(backlog.items):
            backlog.items = kept
            backlog.save(ws.backlog_file)


def used_backlog_numbers(ws: Workspace) -> set[int]:
    """Backlog numbers that projects' goal.md files point at ("Backlog item: #N")."""
    numbers = set()
    for slug in ws.project_slugs():
        value = goal_fields(ws, slug).get("backlog item", "").removeprefix("#")
        if value.isdigit():
            numbers.add(int(value))
    return numbers


def used_coordinator_names(ws: Workspace) -> set[str]:
    """Coordinator names that projects' goal.md files already use."""
    names = set()
    for slug in ws.project_slugs():
        project = load_project(ws, slug)
        if project and project.coordinator:
            names.add(project.coordinator)
    return names


def projects_on_repo(ws: Workspace, repo: str, exclude: str = "") -> list[tuple[Project, str]]:
    """Other Jef projects that use this repo, with their backlog status. If a
    finished project's backlog row has been deleted, goal.md still says Done."""
    backlog = {f"#{i.number}": i.status.value for i in Backlog.load(ws.backlog_file).items}
    found = []
    for slug in ws.project_slugs():
        if slug == exclude:
            continue
        project = load_project(ws, slug)
        if project and project.repo.lower() == repo.lower():
            fields = goal_fields(ws, slug)
            status = backlog.get(fields.get("backlog item", ""), "")
            if not status and fields.get("done", "").lower() == "yes":
                status = BacklogStatus.DONE.value
            found.append((project, status))
    return found


def known_repos(ws: Workspace) -> list[str]:
    """Every repo a Jef project has used, most recently started first."""
    dated = []
    for slug in ws.project_slugs():
        fields = goal_fields(ws, slug)
        if fields.get("repo"):
            dated.append((fields.get("started", ""), fields["repo"]))
    seen, repos = set(), []
    for _, repo in sorted(dated, reverse=True):
        if repo.lower() not in seen:
            seen.add(repo.lower())
            repos.append(repo)
    return repos


def first_message(item: BacklogItem, repo: str, coordinator: str, others: list[tuple[Project, str]] = ()) -> str:
    message = (
        f"Hi {coordinator}. You're the coordinator for a new project.\n\n"
        f"The request: {item.feature}\n"
        f"The repo: {repo} (your working folder is a fresh clone of it)\n\n"
    )
    if others:
        message += (
            "This repo is already part of other Jef projects. Their work may already be in the "
            "repo, or still in open PRs, so check for overlap and build on it:\n"
            + "".join(f"- {p.name} (coordinator {p.coordinator}, {status or 'status unknown'})\n" for p, status in others)
            + "\n"
        )
    return message + (
        "Follow your coordinator instructions: look around the repo, then reply with "
        "what you understand, what in the repo matters, and any questions."
    )


def start_project(
    ws: Workspace,
    number: int,
    repo_text: str,
    chats: Chats,
    clone: Callable[[str, Path], None] = gh_clone,
    choose_name: Callable[[list[str]], str] = random.choice,
) -> Project:
    backlog = Backlog.load(ws.backlog_file)
    item = next((i for i in backlog.items if i.number == number), None)
    if item is None:
        raise StartError(f"There's no backlog item #{number}.")
    if item.status is not BacklogStatus.WAITING:
        raise StartError(f"Item #{number} has already been started.")
    repo = parse_repo(repo_text)

    slug = slugify(item.feature, set(ws.project_slugs()))
    coordinator = pick_coordinator_name(backlog, choose_name, used_coordinator_names(ws))
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
            first_message(item, repo, coordinator, projects_on_repo(ws, repo, exclude=slug)),
            folder / "repo",
            instructions=skills.load("coordinator"),
        )
        from jef.status import write_status  # status.py imports this module

        write_status(ws, slug, load_roster(ws, slug))
    except (StartError, ChatError, OSError, subprocess.SubprocessError) as err:
        shutil.rmtree(folder, ignore_errors=True)  # leave nothing half-started
        raise err if isinstance(err, StartError) else StartError(str(err)) from err

    item.status = BacklogStatus.IN_PROGRESS
    item.coordinator = coordinator
    backlog.save(ws.backlog_file)
    return Project(slug, item.feature, repo, coordinator)
