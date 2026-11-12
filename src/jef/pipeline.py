"""What happens after you start a project, from "go" to merged PRs.

A project moves through these phases. Jef works out the phase from the
project's files, so there's no hidden state:

    Understanding  you and the coordinator talk until you say go
    Research       a research agent studies the repo and writes research.md
    Planning       the coordinator turns research.md into tasks.md
    Approval       you look at the tasks and approve them (or ask for changes);
                   if you haven't by the next heartbeat, the coordinator decides
                   (unless that's turned off for the project)
    Building       one implementer per task, each on its own branch, opens a PR;
                   after each merge the coordinator decides when the next starts
                   (unless that's turned off, then Jef starts it straight away)
    Done           every task's PR is merged

The heartbeat (``Heartbeat``) ticks every few seconds. On each tick it reads
the replies agents finished since last time and acts on them, starts
implementers for tasks that are ready, and, once per heartbeat interval
(an hour by default), nudges implementers that are still working and checks
GitHub for merged PRs.

Agents only reply in text. Jef reads a few plain markers in those replies:

- the coordinator ends with ``ACTION: start-research`` when you say go,
- the coordinator puts the task list in a fenced ``tasks`` block,
- the coordinator ends with ``ACTION: approve-tasks`` to approve a waiting list,
- after a PR merges, the coordinator ends with ``ACTION: start-task <n>`` to start
  the next task (after revising the remaining tasks in a ``tasks`` block, if
  needed), or ``ACTION: wait <reason>`` if it needs you first,
- implementers end with ``STATUS: working | ready | stuck <reason>``; with ``ready``
  they include a ``pr`` block, and Jef pushes the branch and opens the PR itself.

Jef writes research.md and tasks.md itself, from those replies.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
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
from jef.publish import Publisher, PublishError
from jef.status import Phase, project_phase, write_status
from jef.tasks import Task, TaskList, TaskStatus
from jef.workspace import Workspace

log = logging.getLogger("jef.pipeline")

RESEARCH_AGENT = "research"
TICK_SECONDS = 15
PR_CHECK_SECONDS = 300  # merged PRs are noticed within five minutes
# Tasks usually build on each other, so by default one task is in flight at a
# time: the next starts once the last one's PR is merged (and the coordinator
# says so).
IMPLEMENTERS_AT_ONCE = 1
MAX_CONTEXT_CHARS = 30_000

ACTION_START_RESEARCH = re.compile(r"^\s*ACTION:\s*start-research\s*$", re.I | re.M)
ACTION_APPROVE_TASKS = re.compile(r"^\s*ACTION:\s*approve-tasks\s*$", re.I | re.M)
ACTION_START_TASK = re.compile(r"^\s*ACTION:\s*start-task\s+#?(\d+)\s*$", re.I | re.M)
ACTION_WAIT = re.compile(r"^\s*ACTION:\s*wait\b[ \t:]*(.*?)\s*$", re.I | re.M)
# The Problem field says this when the coordinator asked to wait for you.
WAITING = "is waiting for you: "
TASKS_BLOCK = re.compile(r"```tasks\s*\n(.*?)```", re.S | re.I)
TASK_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
STATUS_LINE = re.compile(r"^\s*STATUS:\s*(working|ready|pr-opened|stuck)\b(.*)$", re.I | re.M)
PR_BLOCK = re.compile(r"```pr\s*\n(.*?)```", re.S | re.I)
MAX_PUBLISH_ATTEMPTS = 3
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


class PipelineError(RuntimeError):
    pass


# ---- reading agent replies


def parse_numbered_tasks(text: str) -> list[tuple[int, str]]:
    """(number, title) for each task in the last ```tasks block in a reply."""
    blocks = TASKS_BLOCK.findall(text)
    if not blocks:
        return []
    found = []
    for line in blocks[-1].splitlines():
        match = TASK_LINE.match(line)
        if match:
            found.append((int(match.group(1)), match.group(2)))
    return found


def parse_tasks(text: str) -> list[str]:
    """Task titles from the last ```tasks block in a reply."""
    return [title for _, title in parse_numbered_tasks(text)]


def parse_start_task(text: str) -> int | None:
    found = ACTION_START_TASK.findall(text)
    return int(found[-1]) if found else None


def parse_wait(text: str) -> str | None:
    """The reason in the last ``ACTION: wait <reason>`` line, or None."""
    found = ACTION_WAIT.findall(text)
    return (found[-1].strip() or "no reason given") if found else None


def pr_label(url: str) -> str:
    """"PR #30" for a GitHub PR link."""
    number = url.rstrip("/").rsplit("/", 1)[-1]
    return f"PR #{number}" if number.isdigit() else "the PR"


def parse_pr(text: str) -> tuple[str, str] | None:
    """(title, body) from the last ```pr block in a reply."""
    blocks = PR_BLOCK.findall(text)
    if not blocks:
        return None
    lines = blocks[-1].strip().splitlines()
    title = ""
    if lines and lines[0].lower().startswith("title:"):
        title = lines.pop(0)[len("title:"):].strip()
    return title, "\n".join(lines).strip()


def parse_status(text: str) -> tuple[str, str] | None:
    """The last STATUS line: ("working" | "pr-opened" | "stuck", the rest)."""
    found = STATUS_LINE.findall(text)
    if not found:
        return None
    kind, rest = found[-1]
    return kind.lower(), rest.strip()


def without_markers(text: str) -> str:
    text = STATUS_LINE.sub("", text)
    text = ACTION_APPROVE_TASKS.sub("", text)
    text = ACTION_START_TASK.sub("", text)
    text = ACTION_WAIT.sub("", text)
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

    def fetch(self, repo: Path) -> None:
        """Bring the clone's view of GitHub up to date (it doesn't touch any files)."""
        self._git(repo, "fetch", "--quiet", "origin")


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
    publisher: Publisher = field(default_factory=Publisher)
    pr_state: Callable[[str], str] = gh_pr_state
    now: Callable[[], float] = time.time
    _last_beat: dict[str, float] = field(default_factory=dict)
    _last_pr_check: dict[str, float] = field(default_factory=dict)
    _last_checked: dict[str, float] = field(default_factory=dict)
    # When the coordinator was asked which task to start next, and whether it
    # has been reminded since.
    _next_asked: dict[str, float] = field(default_factory=dict)
    _next_reminded: dict[str, bool] = field(default_factory=dict)

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

    def coordinator_decides(self, slug: str) -> bool:
        """Whether the coordinator approves or revises a waiting task list at
        the heartbeat. On unless goal.md says ``Coordinator decides: no``."""
        return goal_fields(self.workspace, slug).get("coordinator decides", "").lower() != "no"

    def set_coordinator_decides(self, slug: str, on: bool) -> None:
        set_goal_field(self.workspace, slug, "Coordinator decides", "yes" if on else "no")
        if on:
            # Turning it on gives you a full interval before the coordinator is asked.
            self._last_beat[slug] = self.now()

    def coordinator_starts(self, slug: str) -> bool:
        """Whether the coordinator decides when the next task starts after a
        merge. On unless goal.md says ``Coordinator starts tasks: no``; then
        Jef starts the next task as soon as the last PR is merged."""
        return goal_fields(self.workspace, slug).get("coordinator starts tasks", "").lower() != "no"

    def set_coordinator_starts(self, slug: str, on: bool) -> None:
        with self.chats.lock:
            set_goal_field(self.workspace, slug, "Coordinator starts tasks", "yes" if on else "no")
            if not on:
                set_goal_field(self.workspace, slug, "Next task asked", "no")
                if WAITING in self.problem(slug):
                    set_goal_field(self.workspace, slug, "Problem", "none")

    def phase(self, slug: str) -> Phase:
        return project_phase(self.workspace, slug, self.chats.refresh(slug))

    def write_status(self, slug: str) -> bool:
        """Rewrite projects/<slug>/status.md from the files, if it changed."""
        with self.chats.lock:
            return write_status(self.workspace, slug, self.chats.refresh(slug))

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
            self.write_status(slug)

    def approve_tasks(self, slug: str) -> None:
        with self.chats.lock:
            if self.phase(slug) is not Phase.APPROVAL:
                raise PipelineError("There are no tasks waiting for approval.")
            set_goal_field(self.workspace, slug, "Tasks approved", "yes")
            self._build(slug)
            self.write_status(slug)

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
            if phase is Phase.APPROVAL:
                self._ask_for_decision(slug)
            if phase is Phase.BUILDING:
                now = self.now()
                if now - self._last_pr_check.get(slug, 0) >= PR_CHECK_SECONDS:
                    self._last_pr_check[slug] = now
                    self._check_prs(slug)
                if now - self._last_beat.get(slug, 0) >= self.heartbeat_minutes(slug) * 60:
                    self._last_beat[slug] = now
                    self._nudge(slug)
                self._build(slug)

    def check_now(self, slug: str) -> None:
        """The "Check status now" button: the heartbeat's checks, straight away.

        Reads finished replies, asks GitHub about every PR waiting for review and
        starts the next task, but doesn't nudge implementers early, so it costs
        no tokens.
        """
        with self.chats.lock:
            if load_project(self.workspace, slug) is None:
                raise PipelineError("There's no such project.")
            if self.phase(slug) in (Phase.DONE, Phase.STOPPED):
                raise PipelineError("This project is finished, so there's nothing to check.")
            # Make tick check PRs after it has read replies, so a PR opened by a
            # reply it reads now is checked too. _last_beat is left alone.
            self._last_pr_check[slug] = float("-inf")
            self.tick(slug)
            self.write_status(slug)
            self._last_checked[slug] = self.now()

    def last_checked(self, slug: str) -> float | None:
        """When you last pressed "Check status now", if Jef has run since."""
        return self._last_checked.get(slug)

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
                # You get a full heartbeat to approve by hand before the
                # coordinator is asked; a revised list restarts the wait.
                self._last_beat[slug] = self.now()
            elif ACTION_APPROVE_TASKS.search(reply.text) and phase is Phase.APPROVAL:
                # The phase is from before this reply, so a list proposed and
                # approved in one go is never approved: it waits at least once.
                if self.coordinator_decides(slug):
                    self.approve_tasks(slug)
                    agent.note = "Approved the tasks"
                else:
                    agent.note = "Tried to approve the tasks, but that's turned off: approve them yourself"
            if phase is Phase.BUILDING:
                self._on_building_reply(slug, agent, reply.text)
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
        if status is None:
            agent.note = "Replied without a status line"
            return
        kind, rest = status
        if kind in ("ready", "pr-opened"):
            self._publish(slug, agent, text)
        elif kind == "stuck":
            agent.note = "Stuck: " + _short(rest or "no reason given")
        else:
            agent.note = "Working" + (": " + _short(rest) if rest else "")

    def _publish(self, slug: str, agent: Agent, text: str) -> None:
        """The implementer says it's done: Jef pushes the branch and opens the PR."""
        tasks = self.tasks(slug)
        task = next((t for t in tasks.tasks if t.agent == agent.name), None)
        if task is None:
            agent.note = "Said it's ready, but has no task"
            return
        pr = parse_pr(text)
        if pr is None and not task.pr:
            self._send_back(slug, agent, "There's no ```pr block with the PR title and description. Add one and report ready again.")
            return
        title, body = pr if pr else (task.title, "")
        try:
            url = self.publisher.publish(
                Path(agent.folder), self._branch(slug, task), self.git.default_branch(self.folder(slug) / "repo"),
                title or task.title, body or task.title,
            )
        except (PublishError, OSError, subprocess.SubprocessError) as err:
            self._send_back(slug, agent, str(err))
            return
        updated = bool(task.pr)
        task.status = TaskStatus.PENDING_REVIEW
        task.pr = url
        tasks.save(self.folder(slug) / "tasks.md")
        agent.note = "PR updated, waiting for your review" if updated else "PR opened, waiting for your review"

    def _send_back(self, slug: str, agent: Agent, problem: str) -> None:
        """Tell the implementer what to fix, a few times at most."""
        match = re.match(r"Couldn't open the PR \((\d+)\)", agent.note)
        attempt = int(match.group(1)) + 1 if match else 1
        if attempt > MAX_PUBLISH_ATTEMPTS:
            agent.note = "Stuck: Jef couldn't open the PR after several tries: " + _short(problem)
            return
        agent.note = f"Couldn't open the PR ({attempt}): " + _short(problem)
        try:
            self.chats.send(slug, agent.name, f"Jef couldn't open your PR yet: {problem}", sender="jef")
        except ChatError:
            pass

    def _on_building_reply(self, slug: str, agent: Agent, text: str) -> None:
        """While building, the coordinator can start the next task (after
        revising the tasks that haven't started, if needed), or ask to wait for
        you. A ```tasks block on its own, without ``ACTION: start-task``,
        changes nothing, so talking about the plan can't drop tasks by accident."""
        start = parse_start_task(text)
        wait = parse_wait(text)
        if start is None and wait is None:
            return
        if not self.coordinator_starts(slug):
            agent.note = "Tried to start a task, but Jef starts tasks itself for this project"
            return
        notes = []
        if start is not None:
            revised = parse_numbered_tasks(text)
            if revised:
                notes.append(self._revise_remaining(slug, revised))
            notes.append(self._start_for_coordinator(slug, agent, start))
        elif wait is not None:
            project = load_project(self.workspace, slug)
            set_goal_field(self.workspace, slug, "Problem", _short(f"{project.coordinator} {WAITING}{wait}", 300))
            set_goal_field(self.workspace, slug, "Next task asked", "no")
            notes.append("Waiting: " + _short(wait))
        agent.note = ". ".join(notes)

    def _revise_remaining(self, slug: str, revised: list[tuple[int, str]]) -> str:
        """Replace the tasks that haven't started. Started and merged tasks
        stay as they are, whatever the new block says about them."""
        tasks = self.tasks(slug)
        kept = [t for t in tasks.tasks if t.status is not TaskStatus.NOT_STARTED]
        taken = {t.number for t in kept}
        new = []
        for number, title in revised:
            if number not in taken:
                taken.add(number)
                new.append(Task(number, title))
        tasks.tasks = sorted(kept + new, key=lambda t: t.number)
        tasks.save(self.folder(slug) / "tasks.md")
        return f"Revised the remaining tasks ({len(new)} left)"

    def _start_for_coordinator(self, slug: str, coordinator: Agent, number: int) -> str:
        """``ACTION: start-task <n>``: Jef starts it, if it can be started now."""
        tasks = self.tasks(slug)
        task = next((t for t in tasks.tasks if t.number == number), None)
        if task is None:
            return f"Asked to start task {number}, but there's no task {number}"
        if task.status is not TaskStatus.NOT_STARTED:
            return f"Asked to start task {number}, but it's already {task.status.value}"
        busy = next((t for t in tasks.tasks if t.status in (TaskStatus.IMPLEMENTATION, TaskStatus.PENDING_REVIEW)), None)
        if busy is not None:
            return f"Asked to start task {number}, but task {busy.number} isn't merged yet"
        problem = self.problem(slug)
        if problem and WAITING not in problem:
            return f"Asked to start task {number}, but there's a problem to sort out first"
        project = load_project(self.workspace, slug)
        merged = goal_fields(self.workspace, slug).get("last merged", "")
        note = f"Started by {project.coordinator}" + (f" after {pr_label(merged)} merged" if merged else "")
        try:
            self._start_implementer(slug, tasks, task, note)
        except (PipelineError, ChatError, OSError, subprocess.SubprocessError) as err:
            set_goal_field(self.workspace, slug, "Problem", _short(f"Couldn't start task {task.number}: {err}", 300))
            return f"Asked to start task {number}, but Jef couldn't start it"
        if problem:
            set_goal_field(self.workspace, slug, "Problem", "none")  # it was only waiting for this
        set_goal_field(self.workspace, slug, "Next task asked", "no")
        return f"Started task {number}"

    def _ask_for_next_task(self, slug: str) -> None:
        """After a merge: ask the coordinator which task starts next. If it
        hasn't said by the next heartbeat, remind it once; if it still hasn't
        a heartbeat later, stop and show you a problem. Never start silently."""
        project = load_project(self.workspace, slug)
        coordinator = self._agent(slug, project.coordinator_agent)
        if coordinator is None or coordinator.status is not Status.IDLE:
            return  # busy (maybe answering right now); ask on a later tick
        now = self.now()
        if goal_fields(self.workspace, slug).get("next task asked", "").lower() != "yes":
            try:
                self.git.fetch(self.folder(slug) / "repo")
            except (PipelineError, OSError, subprocess.SubprocessError):
                pass  # it can still decide from the PR on GitHub
            try:
                self.chats.send(slug, coordinator.name, self._next_task_message(slug), sender="jef")
            except ChatError:
                return
            set_goal_field(self.workspace, slug, "Next task asked", "yes")
            self._next_asked[slug], self._next_reminded[slug] = now, False
            return
        asked = self._next_asked.setdefault(slug, now)  # after a restart, wait a full interval
        if now - asked < self.heartbeat_minutes(slug) * 60:
            return
        if not self._next_reminded.get(slug):
            try:
                self.chats.send(slug, coordinator.name, self._next_task_reminder(), sender="heartbeat")
            except ChatError:
                return
            self._next_asked[slug], self._next_reminded[slug] = now, True
            return
        set_goal_field(
            self.workspace, slug, "Problem",
            f"{project.coordinator} hasn't said which task to start next. Ask in the chat, or press Try again to ask again.",
        )
        set_goal_field(self.workspace, slug, "Next task asked", "no")

    def _branch(self, slug: str, task: Task) -> str:
        return f"jef/{slug}/task-{task.number}"

    def _ask_for_tasks(self, slug: str) -> None:
        if goal_fields(self.workspace, slug).get("planning requested", "").lower() == "yes":
            return
        project = load_project(self.workspace, slug)
        coordinator = self._agent(slug, project.coordinator_agent)
        if coordinator is None or coordinator.status is not Status.IDLE:
            return  # busy talking to you; ask on a later tick
        self.chats.send(slug, coordinator.name, self._planning_message(slug), sender="jef")
        set_goal_field(self.workspace, slug, "Planning requested", "yes")

    def _ask_for_decision(self, slug: str) -> None:
        """Once per heartbeat while the tasks wait, ask the coordinator to decide."""
        if not self.coordinator_decides(slug):
            return  # turned off on the project page: only you approve
        now = self.now()
        # After a restart, wait a full interval rather than asking at once.
        self._last_beat.setdefault(slug, now)
        minutes = self.heartbeat_minutes(slug)
        if now - self._last_beat[slug] < minutes * 60:
            return
        project = load_project(self.workspace, slug)
        coordinator = self._agent(slug, project.coordinator_agent)
        if coordinator is None or coordinator.status is not Status.IDLE:
            return  # busy talking to you; ask on a later tick
        try:
            self.chats.send(slug, coordinator.name, self._decision_message(slug, now), sender="heartbeat")
        except ChatError:
            return
        self._last_beat[slug] = now

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
            if task.status is not TaskStatus.NOT_STARTED:
                continue
            merged = goal_fields(self.workspace, slug).get("last merged", "")
            after_merge = any(t.status is TaskStatus.MERGED for t in tasks.tasks)
            if after_merge and self.coordinator_starts(slug):
                # The first tasks start when the list is approved; after that
                # the coordinator looks at what merged and decides.
                self._ask_for_next_task(slug)
                break
            if after_merge:
                note = "Started by Jef" + (f" after {pr_label(merged)} merged" if merged else "")
            else:
                note = "Started by Jef when the tasks were approved"
            try:
                self._start_implementer(slug, tasks, task, note)
            except (PipelineError, ChatError, OSError, subprocess.SubprocessError) as err:
                set_goal_field(self.workspace, slug, "Problem", _short(f"Couldn't start task {task.number}: {err}", 300))
                return
            in_flight.append(task)
        if tasks.tasks and all(t.status is TaskStatus.MERGED for t in tasks.tasks):
            self._finish(slug)

    def _start_implementer(self, slug: str, tasks: TaskList, task: Task, note: str = "") -> None:
        name = f"impl-{task.number}"
        repo = self.folder(slug) / "repo"
        base = self.git.default_branch(repo)
        branch = self._branch(slug, task)
        work = self.folder(slug) / "work" / name
        if not work.exists():
            self.git.new_worktree(repo, work, branch, base)
        project = load_project(self.workspace, slug)
        agent = self.chats.spawn(
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
        if note:
            agent.note = note  # so you can see who started it, and why
            self.chats.update(slug, agent)

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
                set_goal_field(self.workspace, slug, "Last merged", task.pr)
                set_goal_field(self.workspace, slug, "Next task asked", "no")
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

    def _decision_message(self, slug: str, now: float) -> str:
        waited = max(1, round((now - self._last_beat[slug]) / 60))
        return (
            f"Heartbeat: the task list has waited about {waited} minutes without being approved. "
            "Please decide on it now.\n\n"
            "## The tasks waiting for approval\n\n"
            + "\n".join(f"{t.number}. {t.title}" for t in self.tasks(slug).tasks)
            + "\n\n"
            "Choose one:\n"
            "- If the list is good, reply with a one-line reason and end with `ACTION: approve-tasks` "
            "on its own line. Jef then starts building.\n"
            "- If it needs changes, reply with the whole updated list in a ```tasks block. "
            "Don't approve in the same reply; the new list waits for approval again.\n"
            "- If you really need the person's answer first (they asked for changes or a question is "
            "still open), ask it and don't approve. You'll be asked again at the next heartbeat."
        )

    def _next_task_message(self, slug: str) -> str:
        merged = goal_fields(self.workspace, slug).get("last merged", "")
        tasks = self.tasks(slug).tasks
        task = next((t for t in tasks if t.pr == merged), None)
        what = (f"{pr_label(merged)} ({merged})" if merged else "The last task's PR") + (
            f" for task {task.number}, {task.title}," if task else ""
        )
        base = self.git.default_branch(self.folder(slug) / "repo")
        return (
            f"{what} has been merged into `{base}`. Jef has fetched it, so `git log origin/{base}` "
            f"in your folder shows it, and `gh pr view {merged}` shows what it changed.\n\n"
            "## The tasks now\n\n"
            + "\n".join(f"{t.number}. {t.title} ({t.status.value})" for t in tasks)
            + "\n\n"
            "Decide what happens next. Choose one:\n"
            "- If the next task still makes sense as written, reply with a one-line reason and end "
            "with `ACTION: start-task <number>` on its own line. Jef then starts it.\n"
            "- If the tasks that haven't started need changing (reworded, split, dropped or added "
            "because of what merged), reply with them in a ```tasks block, numbered after the tasks "
            "that have started, then end with `ACTION: start-task <number>`.\n"
            "- If you need the person's answer first, ask it and end with `ACTION: wait <reason>`. "
            "Jef shows the reason on the project page and starts nothing."
        )

    def _next_task_reminder(self) -> str:
        return (
            "Heartbeat: Jef is still waiting for you to say which task starts next. End your reply with "
            "`ACTION: start-task <number>` or `ACTION: wait <reason>`. If there's no answer by the next "
            "heartbeat, Jef stops and asks the person."
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
            "When it's done and tested, commit it and report `STATUS: ready` with a ```pr block. "
            "Don't push or open the PR yourself: Jef checks your commits for secrets, pushes your "
            "branch and opens the PR. Follow your implementer instructions, and end every reply "
            "with one STATUS line."
        )

    def _nudge_message(self, task: Task) -> str:
        return (
            f"Heartbeat check-in for task {task.number}: {task.title}.\n"
            "Carry on from where you left off. When everything is committed and tested, report "
            "`STATUS: ready` with a ```pr block. End your reply with one STATUS line."
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
            try:
                # Done and Stopped projects too, so their last status is written.
                self.pipeline.write_status(slug)
            except Exception:
                log.exception("Couldn't write status.md for project %s", slug)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            self.tick_all()
