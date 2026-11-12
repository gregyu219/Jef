"""What happens after you start a project, from "go" to merged PRs.

A project moves through these phases. Jef works out the phase from the
project's files, so there's no hidden state:

    Understanding  you and the coordinator talk until you say go
    Research       a research agent studies the repo and writes research.md
    Planning       the coordinator turns research.md into tasks.md
    Approval       you look at the tasks and approve them (or ask for changes)
    Building       one implementer per task, each on its own branch, opens a PR
    Done           every task's PR is merged

The heartbeat (``Heartbeat``) ticks every few seconds. On each tick it reads
the replies agents finished since last time and acts on them, starts
implementers for tasks that are ready, and, once per heartbeat interval
(an hour by default), nudges implementers that are still working and checks
GitHub for merged PRs.

Agents only reply in text. Jef reads a few plain markers in those replies:

- the coordinator ends with ``ACTION: start-research`` when you say go,
- the coordinator puts the task list in a fenced ``tasks`` block,
- implementers end with ``STATUS: working | pr-opened <url> | stuck <reason>``.

Jef writes research.md and tasks.md itself, from those replies.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from jef import skills
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Agent, Chats, ChatError, Reply, Role, Status
from jef.projects import (
    DEFAULT_HEARTBEAT_MINUTES,
    agent_name,
    goal_fields,
    load_project,
    set_goal_field,
)
from jef.tasks import Task, TaskList, TaskStatus
from jef.workspace import Workspace

log = logging.getLogger("jef.pipeline")

RESEARCH_AGENT = "research"
TICK_SECONDS = 15
PR_CHECK_SECONDS = 300  # merged PRs are noticed within five minutes
# Tasks usually build on each other, so by default one task is in flight at a
# time: the next starts once the last one's PR is merged.
IMPLEMENTERS_AT_ONCE = 1
MAX_CONTEXT_CHARS = 30_000

ACTION_START_RESEARCH = re.compile(r"^\s*ACTION:\s*start-research\s*$", re.I | re.M)
TASKS_BLOCK = re.compile(r"```tasks\s*\n(.*?)```", re.S | re.I)
TASK_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
STATUS_LINE = re.compile(r"^\s*STATUS:\s*(working|pr-opened|stuck)\b(.*)$", re.I | re.M)
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


class Phase(str, Enum):
    UNDERSTANDING = "Understanding"
    RESEARCH = "Research"
    PLANNING = "Planning"
    APPROVAL = "Waiting for your approval"
    BUILDING = "Building"
    DONE = "Done"
    STOPPED = "Stopped"


class PipelineError(RuntimeError):
    pass


# ---- reading agent replies


def parse_tasks(text: str) -> list[str]:
    """Task titles from the last ```tasks block in a reply."""
    blocks = TASKS_BLOCK.findall(text)
    if not blocks:
        return []
    titles = []
    for line in blocks[-1].splitlines():
        match = TASK_LINE.match(line)
        if match:
            titles.append(match.group(2))
    return titles


def parse_status(text: str) -> tuple[str, str] | None:
    """The last STATUS line: ("working" | "pr-opened" | "stuck", the rest)."""
    found = STATUS_LINE.findall(text)
    if not found:
        return None
    kind, rest = found[-1]
    return kind.lower(), rest.strip()


def without_markers(text: str) -> str:
    text = STATUS_LINE.sub("", text)
    return ACTION_START_RESEARCH.sub("", text).strip()


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    return text if len(text) <= limit else "…(earlier part left out)\n" + text[-limit:]


# ---- git and GitHub


@dataclass
class Git:
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def _git(self, repo: Path, *args: str) -> str:
        result = self.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL
        )
        if result.returncode != 0:
            raise PipelineError(f"git {args[0]} failed: {_short(result.stderr or result.stdout)}")
        return result.stdout.strip()

    def default_branch(self, repo: Path) -> str:
        try:
            ref = self._git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            return ref.split("/", 1)[1] if "/" in ref else ref
        except PipelineError:
            return "main"

    def new_worktree(self, repo: Path, path: Path, branch: str, base: str) -> None:
        """A separate checkout on a new branch from the latest base, so
        implementers never trip over each other."""
        self._git(repo, "fetch", "--quiet", "origin")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(repo, "worktree", "add", "--quiet", "-B", branch, str(path), f"origin/{base}")


def gh_pr_state(url: str) -> str:
    """OPEN, MERGED or CLOSED, or "" if GitHub couldn't be asked."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state", "--jq", ".state"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip().upper() if result.returncode == 0 else ""


# ---- the pipeline


@dataclass
class Pipeline:
    workspace: Workspace
    chats: Chats
    git: Git = field(default_factory=Git)
    pr_state: Callable[[str], str] = gh_pr_state
    now: Callable[[], float] = time.time
    _last_beat: dict[str, float] = field(default_factory=dict)
    _last_pr_check: dict[str, float] = field(default_factory=dict)

    # -- reading project state

    def folder(self, slug: str) -> Path:
        return self.workspace.project_dir(slug)

    def tasks(self, slug: str) -> TaskList:
        return TaskList.load(self.folder(slug) / "tasks.md")

    def research(self, slug: str) -> str:
        path = self.folder(slug) / "research.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def heartbeat_minutes(self, slug: str) -> int:
        value = goal_fields(self.workspace, slug).get("heartbeat", "")
        number = value.split()[0] if value else ""
        return int(number) if number.isdigit() and int(number) > 0 else DEFAULT_HEARTBEAT_MINUTES

    def set_heartbeat_minutes(self, slug: str, minutes: int) -> None:
        if not 5 <= minutes <= 24 * 60:
            raise PipelineError("Pick a heartbeat between 5 minutes and 24 hours.")
        set_goal_field(self.workspace, slug, "Heartbeat", f"{minutes} minutes")

    def phase(self, slug: str) -> Phase:
        fields = goal_fields(self.workspace, slug)
        if fields.get("done", "").lower() == "yes":
            return Phase.DONE
        project = load_project(self.workspace, slug)
        coordinator = self._agent(slug, project.coordinator_agent) if project else None
        if coordinator is not None and coordinator.status is Status.STOPPED:
            return Phase.STOPPED
        if fields.get("tasks approved", "").lower() == "yes":
            return Phase.BUILDING
        if self.tasks(slug).tasks:
            return Phase.APPROVAL
        research = fields.get("research", "").lower()
        if research == "done":
            return Phase.PLANNING
        if research == "started":
            return Phase.RESEARCH
        return Phase.UNDERSTANDING

    def _agent(self, slug: str, name: str) -> Agent | None:
        return next((a for a in self.chats.refresh(slug) if a.name == name), None)

    # -- things you (or the coordinator) trigger

    def start_research(self, slug: str) -> None:
        with self.chats.lock:
            if self.phase(slug) is not Phase.UNDERSTANDING:
                raise PipelineError("Research has already started for this project.")
            project = load_project(self.workspace, slug)
            self.chats.spawn(
                slug,
                RESEARCH_AGENT,
                Role.RESEARCH,
                self._research_message(slug),
                self.folder(slug) / "repo",
                parent=project.coordinator_agent,
                instructions=skills.load("research"),
            )
            set_goal_field(self.workspace, slug, "Research", "started")

    def approve_tasks(self, slug: str) -> None:
        with self.chats.lock:
            if self.phase(slug) is not Phase.APPROVAL:
                raise PipelineError("There are no tasks waiting for approval.")
            set_goal_field(self.workspace, slug, "Tasks approved", "yes")
            self._build(slug)

    # -- the heartbeat

    def tick(self, slug: str) -> None:
        with self.chats.lock:
            if load_project(self.workspace, slug) is None or self.phase(slug) in (Phase.DONE, Phase.STOPPED):
                return
            for agent in self.chats.refresh(slug):
                if agent.status is Status.WORKING:
                    continue
                replies = self.chats.replies(slug, agent.name)
                for reply in replies[agent.handled:]:
                    self._on_reply(slug, agent, reply)
                    agent.handled += 1
                    self.chats.update(slug, agent)
            phase = self.phase(slug)
            if phase is Phase.PLANNING:
                self._ask_for_tasks(slug)
            if phase is Phase.BUILDING:
                now = self.now()
                if now - self._last_pr_check.get(slug, 0) >= PR_CHECK_SECONDS:
                    self._last_pr_check[slug] = now
                    self._check_prs(slug)
                if now - self._last_beat.get(slug, 0) >= self.heartbeat_minutes(slug) * 60:
                    self._last_beat[slug] = now
                    self._nudge(slug)
                self._build(slug)

    def _on_reply(self, slug: str, agent: Agent, reply: Reply) -> None:
        if reply.is_error:
            agent.note = "Last turn failed: " + _short(reply.text)
            return
        if agent.role is Role.COORDINATOR:
            phase = self.phase(slug)
            if ACTION_START_RESEARCH.search(reply.text) and phase is Phase.UNDERSTANDING:
                self.start_research(slug)
                agent.note = "Started research"
            titles = parse_tasks(reply.text)
            if titles and phase in (Phase.PLANNING, Phase.APPROVAL):
                tasks = self.tasks(slug)
                tasks.tasks = [Task(i, title) for i, title in enumerate(titles, start=1)]
                tasks.save(self.folder(slug) / "tasks.md")
                agent.note = f"Proposed {len(titles)} tasks"
        elif agent.role is Role.RESEARCH:
            findings = without_markers(reply.text)
            if findings and goal_fields(self.workspace, slug).get("research", "").lower() == "started":
                (self.folder(slug) / "research.md").write_text(findings + "\n", encoding="utf-8")
                set_goal_field(self.workspace, slug, "Research", "done")
                agent.note = "Findings written to research.md"
        elif agent.role is Role.IMPLEMENTER:
            self._on_implementer_reply(slug, agent, reply.text)

    def _on_implementer_reply(self, slug: str, agent: Agent, text: str) -> None:
        status = parse_status(text)
        tasks = self.tasks(slug)
        task = next((t for t in tasks.tasks if t.agent == agent.name), None)
        if status is None:
            agent.note = "Replied without a status line"
            return
        kind, rest = status
        if kind == "pr-opened":
            url = PR_URL.search(rest) or PR_URL.search(text)
            if url and task is not None:
                task.status = TaskStatus.PENDING_REVIEW
                task.pr = url.group(0)
                tasks.save(self.folder(slug) / "tasks.md")
                agent.note = "PR opened, waiting for your review"
            else:
                agent.note = "Said it opened a PR but gave no PR link"
        elif kind == "stuck":
            agent.note = "Stuck: " + _short(rest or "no reason given")
        else:
            agent.note = "Working" + (": " + _short(rest) if rest else "")

    def _ask_for_tasks(self, slug: str) -> None:
        if goal_fields(self.workspace, slug).get("planning requested", "").lower() == "yes":
            return
        project = load_project(self.workspace, slug)
        coordinator = self._agent(slug, project.coordinator_agent)
        if coordinator is None or coordinator.status is not Status.IDLE:
            return  # busy talking to you; ask on a later tick
        self.chats.send(slug, coordinator.name, self._planning_message(slug), sender="jef")
        set_goal_field(self.workspace, slug, "Planning requested", "yes")

    def problem(self, slug: str) -> str:
        """Something that stopped the pipeline and needs you, or ""."""
        value = goal_fields(self.workspace, slug).get("problem", "")
        return "" if value.lower() in ("", "none") else value

    def clear_problem(self, slug: str) -> None:
        with self.chats.lock:
            set_goal_field(self.workspace, slug, "Problem", "none")
            self.tick(slug)

    def _build(self, slug: str) -> None:
        if self.problem(slug):
            return  # wait for you to press Try again
        tasks = self.tasks(slug)
        in_flight = [t for t in tasks.tasks if t.status in (TaskStatus.IMPLEMENTATION, TaskStatus.PENDING_REVIEW)]
        for task in tasks.tasks:
            if len(in_flight) >= IMPLEMENTERS_AT_ONCE:
                break
            if task.status is TaskStatus.NOT_STARTED:
                try:
                    self._start_implementer(slug, tasks, task)
                except (PipelineError, ChatError, OSError, subprocess.SubprocessError) as err:
                    set_goal_field(self.workspace, slug, "Problem", _short(f"Couldn't start task {task.number}: {err}", 300))
                    return
                in_flight.append(task)
        if tasks.tasks and all(t.status is TaskStatus.MERGED for t in tasks.tasks):
            self._finish(slug)

    def _start_implementer(self, slug: str, tasks: TaskList, task: Task) -> None:
        name = f"impl-{task.number}"
        repo = self.folder(slug) / "repo"
        base = self.git.default_branch(repo)
        branch = f"jef/{slug}/task-{task.number}"
        work = self.folder(slug) / "work" / name
        if not work.exists():
            self.git.new_worktree(repo, work, branch, base)
        project = load_project(self.workspace, slug)
        self.chats.spawn(
            slug,
            name,
            Role.IMPLEMENTER,
            self._implementer_message(slug, task, branch, base),
            work,
            parent=project.coordinator_agent,
            instructions=skills.load("implementer"),
        )
        task.status = TaskStatus.IMPLEMENTATION
        task.agent = name
        tasks.save(self.folder(slug) / "tasks.md")

    def _check_prs(self, slug: str) -> None:
        tasks = self.tasks(slug)
        changed = False
        for task in tasks.tasks:
            if task.status is not TaskStatus.PENDING_REVIEW or not task.pr:
                continue
            state = self.pr_state(task.pr)
            agent = self._agent(slug, task.agent) if task.agent else None
            if state == "MERGED":
                task.status = TaskStatus.MERGED
                changed = True
                if agent is not None and agent.status is not Status.STOPPED:
                    self.chats.stop(slug, agent.name)
            elif state == "CLOSED" and agent is not None:
                agent.note = "PR was closed without merging. Tell the coordinator what to do."
                self.chats.update(slug, agent)
        if changed:
            tasks.save(self.folder(slug) / "tasks.md")

    def _nudge(self, slug: str) -> None:
        """Hourly check-in for implementers that haven't handed back a PR yet."""
        tasks = {t.agent: t for t in self.tasks(slug).tasks if t.agent}
        for agent in self.chats.refresh(slug):
            task = tasks.get(agent.name)
            if agent.role is not Role.IMPLEMENTER or agent.status is not Status.IDLE or task is None:
                continue
            if task.status is not TaskStatus.IMPLEMENTATION or agent.note.startswith("Stuck"):
                continue  # done, or waiting on you
            try:
                self.chats.send(slug, agent.name, self._nudge_message(task), sender="heartbeat")
            except ChatError:
                pass

    def _finish(self, slug: str) -> None:
        set_goal_field(self.workspace, slug, "Done", "yes")
        project = load_project(self.workspace, slug)
        number = goal_fields(self.workspace, slug).get("backlog item", "").lstrip("#")
        backlog = Backlog.load(self.workspace.backlog_file)
        for item in backlog.items:
            if str(item.number) == number:
                item.status = BacklogStatus.DONE
        backlog.save(self.workspace.backlog_file)
        try:
            self.chats.stop(slug, project.coordinator_agent)
        except ChatError:
            pass

    # -- what agents are told

    def _goal(self, slug: str) -> str:
        return (self.folder(slug) / "goal.md").read_text(encoding="utf-8")

    def _conversation(self, slug: str) -> str:
        project = load_project(self.workspace, slug)
        lines = [
            line.text if line.kind == "sent" else f"{project.coordinator}: {line.text}"
            for line in self.chats.transcript(slug, project.coordinator_agent)
            if line.kind in ("sent", "text")
        ]
        return _tail("\n\n".join(lines))

    def _research_message(self, slug: str) -> str:
        return (
            "You're the research agent for this project. Your working folder is a clone of its repo.\n\n"
            f"## goal.md\n\n{self._goal(slug)}\n"
            "## The conversation so far between the person and the coordinator\n\n"
            f"{self._conversation(slug)}\n\n"
            "Follow your research instructions. Your final reply becomes research.md, so make it the "
            "complete findings in the format your instructions give."
        )

    def _planning_message(self, slug: str) -> str:
        return (
            "The research agent has finished. Its findings, now saved as research.md:\n\n"
            f"{_tail(self.research(slug))}\n\n"
            "Turn this into tasks, following your planning instructions: a short note for the person, "
            "then the numbered task list inside a ```tasks block."
        )

    def _implementer_message(self, slug: str, task: Task, branch: str, base: str) -> str:
        return (
            f"You're the implementation agent for task {task.number}: {task.title}\n\n"
            f"Your working folder is a checkout of the repo on your own branch, `{branch}`, "
            f"made from the latest `{base}`.\n\n"
            f"## goal.md\n\n{self._goal(slug)}\n"
            f"## research.md\n\n{_tail(self.research(slug))}\n\n"
            "## All tasks, so you know what's yours and what isn't\n\n"
            + "\n".join(f"{t.number}. {t.title}" for t in self.tasks(slug).tasks)
            + "\n\n"
            f"When it's done and tested, push `{branch}` and open the PR with "
            f"`gh pr create --base {base}`. Follow your implementer instructions, and end every "
            "reply with one STATUS line."
        )

    def _nudge_message(self, task: Task) -> str:
        return (
            f"Heartbeat check-in for task {task.number}: {task.title}.\n"
            "Carry on from where you left off. If the PR is already open, reply with "
            "`STATUS: pr-opened <PR URL>`. End your reply with one STATUS line."
        )


class Heartbeat:
    """Ticks every project's pipeline in a background thread while Jef runs."""

    def __init__(self, pipeline: Pipeline, every: float = TICK_SECONDS):
        self.pipeline = pipeline
        self.every = every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="jef-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def tick_all(self) -> None:
        for slug in self.pipeline.workspace.project_slugs():
            try:
                self.pipeline.tick(slug)
            except Exception:  # One broken project mustn't stop the others.
                log.exception("Heartbeat failed for project %s", slug)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            self.tick_all()
