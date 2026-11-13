"""How a project runs, from "go" to merged PRs.

**The coordinator runs its project.** It talks with you, starts research,
plans the tasks, starts implementers (up to N tasks at a time, set per
project), nudges them, checks their PRs on GitHub (merged? conflicts?) and
sends work back. It does all of that with its own ``jef`` commands (``jef
task start 1``, ``jef prs check``; see ``jef.coordinate``), which Jef carries
out from its inbox (``jef.inbox``). Jef doesn't decide anything itself: the
one thing it does on its own is start tasks the coordinator's plan says are
ready (every task they come after has merged), up to the limit.

**Jef only keeps time and keeps the rules.** While it runs, it:

- carries out coordinators' requests within a few seconds, refusing anything
  against the rules (no more tasks at once than the project allows, only the
  coordinator's own agents, secrets never pushed, never ``main``),
- wakes each coordinator once per heartbeat (an hour by default), or sooner
  when one of its agents finishes a turn, a PR merges or you press a button,
- every few minutes, asks GitHub (with ``gh``, no LLM) whether each PR
  waiting for review has merged; a merge is recorded as ``jef prs check``
  would, and the coordinator is woken,
- starts each task with nothing left to wait for (``[after N]`` in its
  title, see ``Pipeline.plan``), up to the project's limit,
- saves the research agent's findings as research.md, and
- reads the files to keep status.md up to date. That costs nothing: no LLM
  call and no GitHub call.

A coordinator can't wake itself: each agent is a Claude Code session that
only runs while it answers a message. That's the one thing Jef's timer is for.

The phase comes from the project's files, so there's no hidden state:

    Understanding  you and the coordinator talk until you say go
    Research       a research agent studies the repo and writes research.md
    Planning       the coordinator turns research.md into tasks.md
    Approval       you approve the tasks (or, if it's allowed and you haven't
                   by the next heartbeat, the coordinator does)
    Building       the coordinator starts one implementer per task; Jef pushes
                   each finished branch and opens its PR
    Done           every task's PR is merged
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

from jef import inbox, models, skills, usage
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Agent, Chats, ChatError, Role, Status, load_roster
from jef.projects import (
    DEFAULT_HEARTBEAT_MINUTES,
    MAX_IMPLEMENTERS_AT_ONCE,
    auto_approve,
    goal_fields,
    implementers_at_once,
    load_project,
    set_goal_field,
)
from jef.publish import Publisher, PublishError
from jef.status import WAITING, Phase, project_phase, render_status, write_status
from jef.tasks import Task, TaskList, TaskStatus, format_after
from jef.workspace import Workspace

log = logging.getLogger("jef.pipeline")

RESEARCH_AGENT = "research"
INBOX_SECONDS = 2  # how often Jef looks for coordinators' requests
TICK_SECONDS = 30  # how often Jef re-reads the files and sees who to wake
PR_POLL_SECONDS = 5 * 60  # how often Jef asks GitHub whether a PR waiting for review merged
MAX_CONTEXT_CHARS = 30_000
MAX_READ_CHARS = 12_000

PR_BLOCK = re.compile(r"```pr\s*\n(.*?)```", re.S | re.I)
BUDGET_PROBLEM = "Budget reached"  # how a budget pause starts, so raising the budget can lift it
MAX_BUDGET_USD = 100_000
# Only tasks being built count toward "Implementers at once"; a PR waiting for review doesn't.
BUILDING = (TaskStatus.IMPLEMENTATION,)
# A task title can end with the tasks it needs merged first: "Add the page [after 1, 2]".
AFTER = re.compile(r"\s*\[after\s+([\d,\s]+)\]\s*$", re.I)


class PipelineError(RuntimeError):
    """A request Jef refused or couldn't do; the message says why, plainly."""


# ---- small helpers


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


def pr_label(url: str) -> str:
    """"PR #30" for a GitHub PR link."""
    number = url.rstrip("/").rsplit("/", 1)[-1]
    return f"PR #{number}" if number.isdigit() else "the PR"


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_after(title: str) -> tuple[str, list[int]]:
    """("Add the page", [1, 2]) from "Add the page [after 1, 2]"."""
    found = AFTER.search(title)
    if not found:
        return title.strip(), []
    numbers = sorted({int(n) for n in found.group(1).replace(",", " ").split()})
    return title[: found.start()].strip(), numbers


def _after_note(task: Task) -> str:
    """" (after 1, 2)" for a task that waits for others, else ""."""
    return f" (after {format_after(task.after)})" if task.after else ""


def _at_limit(numbers: list[int], limit: int) -> str:
    """Why another task can't start: "Tasks 1 and 2 are still being built, …"."""
    if len(numbers) == 1:
        busy, its = f"Task {numbers[0]} is", "its PR is"
    else:
        busy, its = "Tasks " + ", ".join(map(str, numbers[:-1])) + f" and {numbers[-1]} are", "one of their PRs is"
    at_most = "builds 1 task" if limit == 1 else f"builds at most {limit} tasks"
    return f"{busy} still being built, and this project {at_most} at a time. Start the next one once {its} open."


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


def _gh_field(url: str, name: str) -> str:
    try:
        result = subprocess.run(
            ["gh", "pr", "view", url, "--json", name, "--jq", f".{name}"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip().upper() if result.returncode == 0 else ""


def gh_pr_state(url: str) -> str:
    """OPEN, MERGED or CLOSED, or "" if GitHub couldn't be asked."""
    return _gh_field(url, "state")


def gh_pr_mergeable(url: str) -> str:
    """MERGEABLE, CONFLICTING or UNKNOWN (GitHub is still working it out), or ""."""
    return _gh_field(url, "mergeable")


# ---- reading agents' replies (also used by ``jef agent read``)


def last_reply(ws: Workspace, slug: str, name: str) -> str:
    """An agent's latest reply, for its coordinator to read. Only reads files
    (agents.md and the chat), so it's safe from outside the running Jef."""
    agent = next((a for a in load_roster(ws, slug) if a.name == name), None)
    if agent is None:
        raise PipelineError(f"There's no agent named {name!r} in this project.")
    replies = Chats(ws, "claude").replies(slug, name)
    if not replies:
        if agent.status is Status.WORKING:
            return f"{name} is still working on its first turn. Check again at your next heartbeat."
        return f"{name} hasn't replied yet."
    prefix = f"({name} is still working on a newer turn; this is its previous reply.)\n\n" if agent.status is Status.WORKING else ""
    reply = replies[-1]
    if reply.is_error:
        return prefix + f"{name}'s last turn failed: {reply.text or reply.subtype}"
    text = reply.text.strip() or "(an empty reply)"
    if len(text) > MAX_READ_CHARS:
        text = "…(start left out)\n" + text[-MAX_READ_CHARS:]
    return prefix + text


# ---- the pipeline


@dataclass
class Pipeline:
    workspace: Workspace
    chats: Chats
    git: Git = field(default_factory=Git)
    publisher: Publisher = field(default_factory=Publisher)
    pr_state: Callable[[str], str] = gh_pr_state
    pr_mergeable: Callable[[str], str] = gh_pr_mergeable
    now: Callable[[], float] = time.time
    _last_wake: dict[str, float] = field(default_factory=dict)
    _events: dict[str, list[str]] = field(default_factory=dict)  # news for the coordinator's next wake
    _last_checked: dict[str, float] = field(default_factory=dict)
    _budget_checked: dict[str, tuple[object, str]] = field(default_factory=dict)
    _last_pr_poll: dict[str, float] = field(default_factory=dict)
    # Tasks being started or published right now. Their git and GitHub calls
    # run without the lock (a fetch can hang for minutes, e.g. across a sleep),
    # so this stops a second request for the same task meanwhile.
    _in_flight: set[tuple[str, int]] = field(default_factory=set)
    _inbox_locks: dict[str, threading.Lock] = field(default_factory=dict)

    # -- reading project state

    def folder(self, slug: str) -> Path:
        return self.workspace.project_dir(slug)

    def tasks(self, slug: str) -> TaskList:
        return TaskList.load(self.folder(slug) / "tasks.md")

    def _save_tasks(self, slug: str, tasks: TaskList) -> None:
        tasks.save(self.folder(slug) / "tasks.md")

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

    def implementers_at_once(self, slug: str) -> int:
        """How many tasks may be in flight (building or waiting for review) at once."""
        return implementers_at_once(self.workspace, slug)

    def set_implementers_at_once(self, slug: str, n: int) -> None:
        if not 1 <= n <= MAX_IMPLEMENTERS_AT_ONCE:
            raise PipelineError(f"Pick between 1 and {MAX_IMPLEMENTERS_AT_ONCE} tasks at once.")
        set_goal_field(self.workspace, slug, "Implementers at once", str(n))

    def coordinator_decides(self, slug: str) -> bool:
        """Whether the coordinator may approve its own task list once it has
        waited a heartbeat for you. On unless goal.md says ``Coordinator decides: no``."""
        return goal_fields(self.workspace, slug).get("coordinator decides", "").lower() != "no"

    def set_coordinator_decides(self, slug: str, on: bool) -> None:
        set_goal_field(self.workspace, slug, "Coordinator decides", "yes" if on else "no")

    def auto_approve(self, slug: str) -> bool:
        """Whether Jef's heartbeat approves the task list by itself once the
        coordinator has posted it and finished its turn. Off unless goal.md
        says ``Auto approve: yes``."""
        return auto_approve(self.workspace, slug)

    def set_auto_approve(self, slug: str, on: bool) -> None:
        with self.chats.lock:
            set_goal_field(self.workspace, slug, "Auto approve", "yes" if on else "no")

    def models(self, slug: str) -> dict[Role, str]:
        """Each level's model for this project: one of ``models.CHOICES``."""
        return {role: models.model_setting(self.workspace, slug, role) for role in models.LEVELS}

    def set_models(self, slug: str, chosen: dict[Role, str]) -> None:
        """Save the chosen models in goal.md. Each agent picks its level's
        model up on its next turn. Nothing is saved if any choice is unknown."""
        cleaned = {}
        for role, value in chosen.items():
            value = value.strip().lower()
            if role not in models.LEVELS or value not in models.CHOICES:
                raise PipelineError(f"Pick one of {', '.join(models.CHOICES)} for the {role.value.lower()} model.")
            cleaned[role] = value
        with self.chats.lock:
            for role, value in cleaned.items():
                set_goal_field(self.workspace, slug, models.LEVELS[role], value)

    def budget_usd(self, slug: str) -> float | None:
        return usage.budget_usd(self.workspace, slug)

    def set_budget_usd(self, slug: str, amount: float | None) -> None:
        """Set the project's dollar budget, or None for no budget. The pause
        starts or lifts straight away."""
        if amount is not None and not 0 < amount <= MAX_BUDGET_USD:
            raise PipelineError(f"Pick a budget above $0 and up to ${MAX_BUDGET_USD:,}, or leave it empty for none.")
        with self.chats.lock:
            set_goal_field(self.workspace, slug, "Budget", f"{amount:.2f} USD" if amount is not None else "none")
            self._budget_checked.pop(slug, None)
            over = self._budget_problem(slug)
            if over:
                set_goal_field(self.workspace, slug, "Problem", over)
            elif self.problem(slug).startswith(BUDGET_PROBLEM):
                set_goal_field(self.workspace, slug, "Problem", "none")

    def _budget_problem(self, slug: str) -> str:
        """Why the project is paused for its budget, or "" if it isn't.
        Adding up the spending reads every chat, so it's only redone when a
        chat file has grown."""
        chats = self.folder(slug) / "chats"
        sizes = tuple(sorted((f.name, f.stat().st_size) for f in chats.glob("*.jsonl"))) if chats.is_dir() else ()
        key = (sizes, goal_fields(self.workspace, slug).get("budget", ""))
        cached = self._budget_checked.get(slug)
        if cached and cached[0] == key:
            return cached[1]
        budget = self.budget_usd(slug)
        problem = ""
        if budget is not None:
            spent = usage.project_total(self.workspace, slug).cost_usd
            if spent >= budget:
                problem = (
                    f"{BUDGET_PROBLEM}: {usage.format_cost(spent)} of {usage.format_cost(budget)} used (estimated). "
                    "Raise the budget to carry on."
                )
        self._budget_checked[slug] = (key, problem)
        return problem

    def phase(self, slug: str) -> Phase:
        return project_phase(self.workspace, slug, self.chats.refresh(slug))

    def write_status(self, slug: str) -> bool:
        """Rewrite projects/<slug>/status.md from the files, if it changed."""
        with self.chats.lock:
            return write_status(self.workspace, slug, self.chats.refresh(slug))

    def problem(self, slug: str) -> str:
        """Something that stopped the project and needs you, or ""."""
        value = goal_fields(self.workspace, slug).get("problem", "")
        return "" if value.lower() in ("", "none") else value

    def _agent(self, slug: str, name: str) -> Agent | None:
        return next((a for a in self.chats.refresh(slug) if a.name == name), None)

    def _coordinator(self, slug: str) -> Agent | None:
        project = load_project(self.workspace, slug)
        return self._agent(slug, project.coordinator_agent) if project else None

    def _tell_coordinator(self, slug: str, news: str) -> None:
        """Queue news for the coordinator's next wake (the next tick, if it's free)."""
        self._events.setdefault(slug, []).append(news)

    # -- your buttons

    def clear_problem(self, slug: str) -> None:
        with self.chats.lock:
            problem = self.problem(slug)
            set_goal_field(self.workspace, slug, "Problem", "none")
            if problem and not problem.startswith(BUDGET_PROBLEM):
                self._tell_coordinator(slug, f"The person pressed Try again on: {problem}")
            self.tick(slug)

    def check_now(self, slug: str) -> None:
        """The "Check status now" button: re-read the files straight away. It
        only wakes the coordinator if there's news for it, so it costs nothing
        otherwise."""
        with self.chats.lock:
            if load_project(self.workspace, slug) is None:
                raise PipelineError("There's no such project.")
            if self.phase(slug) in (Phase.DONE, Phase.STOPPED):
                raise PipelineError("This project is finished, so there's nothing to check.")
            self.process_inbox(slug)
            self.tick(slug)
            self.write_status(slug)
            self._last_checked[slug] = self.now()

    def forget(self, slug: str) -> None:
        """Drop what Jef remembers about a project that has been deleted."""
        with self.chats.lock:
            for remembered in (self._last_wake, self._events, self._last_checked, self._budget_checked, self._last_pr_poll):
                remembered.pop(slug, None)

    def last_checked(self, slug: str) -> float | None:
        """When you last pressed "Check status now", if Jef has run since."""
        return self._last_checked.get(slug)

    def wake_now(self, slug: str) -> None:
        """The "Ask the coordinator to check now" button: a heartbeat straight away."""
        with self.chats.lock:
            project = load_project(self.workspace, slug)
            coordinator = self._coordinator(slug)
            if project is None or coordinator is None or coordinator.status is Status.STOPPED:
                raise PipelineError("This project's coordinator has stopped.")
            if coordinator.status is Status.WORKING:
                raise PipelineError(f"{project.coordinator} is busy right now. Try again when it's done.")
            self._tell_coordinator(slug, "The person asked you to check on the project now.")
            self.tick(slug)

    # -- things you or the coordinator can do. ``by`` is who asked: "you" or
    #    the coordinator's name.

    def start_research(self, slug: str, brief: str = "", by: str = "you") -> str:
        with self.chats.lock:
            if self.phase(slug) is not Phase.UNDERSTANDING:
                raise PipelineError("Research has already started for this project.")
            project = load_project(self.workspace, slug)
            self.chats.spawn(
                slug,
                RESEARCH_AGENT,
                Role.RESEARCH,
                self._research_message(slug, brief),
                self.folder(slug) / "repo",
                parent=project.coordinator_agent,
                instructions=skills.load("research"),
            )
            set_goal_field(self.workspace, slug, "Research", "started")
            if by == "you":
                self._tell_coordinator(slug, "The person pressed Start research, so the research agent is working.")
            self.write_status(slug)
            return "The research agent has started. Jef saves its findings as research.md and wakes you when it's done."

    def plan(self, slug: str, titles: list[str], by: str) -> str:
        """Set the task list. Before approval this replaces it; while building
        it replaces only the tasks that haven't started."""
        planned = [split_after(t) for t in titles if t.strip()]
        titles = [_short(title, 200) for title, _ in planned]
        with self.chats.lock:
            phase = self.phase(slug)
            if phase in (Phase.UNDERSTANDING, Phase.RESEARCH):
                raise PipelineError("Research hasn't finished yet, so there's nothing to plan from.")
            if phase in (Phase.DONE, Phase.STOPPED):
                raise PipelineError("This project is finished.")
            if not titles and phase is not Phase.BUILDING:
                raise PipelineError('Give at least one task, like: jef tasks set "Show the version in the footer"')
            tasks = self.tasks(slug)
            kept = [t for t in tasks.tasks if t.status is not TaskStatus.NOT_STARTED] if phase is Phase.BUILDING else []
            first = max((t.number for t in kept), default=0) + 1
            new = [Task(n, title, after=after) for n, (title, (_, after)) in enumerate(zip(titles, planned), start=first)]
            numbers = {t.number for t in kept + new}
            for task in new:
                for needed in task.after:
                    if needed >= task.number or needed not in numbers:
                        raise PipelineError(
                            f"Task {task.number} ({task.title}) can't come after task {needed}: a task can only "
                            "come after an earlier task in the list. See `jef overview` for the numbers."
                        )
            tasks.tasks = kept + new
            self._save_tasks(slug, tasks)
            # Lists set from now on say what each task waits for, so Jef may start them by itself.
            set_goal_field(self.workspace, slug, "Dependencies", "yes")
            if phase is Phase.BUILDING:
                self.write_status(slug)
                return "Kept the tasks that have started, and set the rest:\n" + self._task_lines(tasks)
            # The list waits for the person: the coordinator may approve it
            # only once it has waited a whole heartbeat.
            set_goal_field(self.workspace, slug, "Tasks proposed", str(int(self.now())))
            self.write_status(slug)
            return "Saved the task list. It now waits for the person's approval:\n" + self._task_lines(tasks)

    def approve_tasks(self, slug: str, by: str = "you") -> str:
        """``by`` is "you", "auto" (the heartbeat, because Auto approve is on)
        or the coordinator's name. Only the coordinator has to wait."""
        with self.chats.lock:
            if self.phase(slug) is not Phase.APPROVAL:
                raise PipelineError("There are no tasks waiting for approval.")
            if by not in ("you", "auto"):
                if not self.coordinator_decides(slug):
                    raise PipelineError("The person turned off coordinator approval for this project. Ask them to approve.")
                proposed = goal_fields(self.workspace, slug).get("tasks proposed", "")
                waited = self.now() - int(proposed) if proposed.isdigit() else float("inf")
                if waited < self.heartbeat_minutes(slug) * 60:
                    raise PipelineError(
                        "The list has to wait one heartbeat for the person before you can approve it. "
                        "Ask them, and decide at your next heartbeat if they haven't."
                    )
            set_goal_field(self.workspace, slug, "Tasks approved", "yes")
            if self.starts_tasks(slug):
                next_step = (
                    "Jef starts every task with nothing left to wait for by itself, up to the limit, "
                    "within a minute."
                )
            else:
                next_step = "Start the first task with `jef task start 1`."
            if by == "you":
                self._tell_coordinator(slug, f"The person approved the tasks. {next_step}")
            elif by == "auto":
                self._tell_coordinator(
                    slug, f"Jef auto-approved the task list (the person turned on Auto approve). {next_step}"
                )
            self.write_status(slug)
            return f"The tasks are approved. {next_step}"

    def starts_tasks(self, slug: str) -> bool:
        """Whether Jef starts this project's unblocked tasks by itself. Only
        for lists set since tasks.md has an After column: in an older list
        every task would look independent."""
        return goal_fields(self.workspace, slug).get("dependencies", "").lower() == "yes"

    def stacked(self, slug: str) -> dict[int, int]:
        """Which tasks were built on another task's unmerged branch, from
        goal.md's ``Stacked`` field ("2 on 1, 3 on 2"): {2: 1, 3: 2}."""
        found = {}
        for part in goal_fields(self.workspace, slug).get("stacked", "").split(","):
            words = part.split()
            if len(words) == 3 and words[1] == "on" and words[0].isdigit() and words[2].isdigit():
                found[int(words[0])] = int(words[2])
        return found

    def _set_stacked(self, slug: str, number: int, on: int) -> None:
        pairs = {**self.stacked(slug), number: on}
        set_goal_field(self.workspace, slug, "Stacked", ", ".join(f"{n} on {m}" for n, m in sorted(pairs.items())))

    def start_ready(self, slug: str) -> list[str]:
        """Jef's own step while building: start every task that's waiting for
        nothing (each task it comes after has merged), in number order, up to
        the project's limit. Never while there's a problem or a ``jef wait``.
        Returns the news it queued for the coordinator."""
        with self.chats.lock:
            if load_project(self.workspace, slug) is None or self.phase(slug) is not Phase.BUILDING:
                return []
            if not self.starts_tasks(slug) or self.problem(slug) or self._budget_problem(slug):
                return []
            tasks = self.tasks(slug).tasks
            merged = {t.number for t in tasks if t.status is TaskStatus.MERGED}
            starting = [n for s, n in self._in_flight if s == slug]
            busy = sum(t.status in BUILDING for t in tasks) + len(starting)
            room = self.implementers_at_once(slug) - busy
            ready = [
                t for t in tasks
                if t.status is TaskStatus.NOT_STARTED and t.number not in starting and set(t.after) <= merged
            ][: max(room, 0)]
        news = []
        # Outside the lock: each start fetches and makes a worktree.
        for task in ready:
            try:
                self.start_task(slug, task.number, by="Jef", lift_wait=False)
            except (PipelineError, ChatError) as err:
                # Someone else started it, or the project changed meanwhile.
                log.info("Jef didn't start task %s in %s: %s", task.number, slug, err)
                continue
            why = "the tasks it comes after have merged" if task.after else "it doesn't wait for any other task"
            item = f"Jef started task {task.number} (impl-{task.number}): {why}."
            with self.chats.lock:
                self._tell_coordinator(slug, item)
            news.append(item)
        return news

    def start_task(self, slug: str, number: int, by: str, on: int | None = None, lift_wait: bool = True) -> str:
        with self.chats.lock:
            if self.phase(slug) is not Phase.BUILDING:
                raise PipelineError("The tasks haven't been approved yet.")
            problem = self.problem(slug)
            if problem and (WAITING not in problem or not lift_wait):
                raise PipelineError(f"There's a problem to sort out first: {problem}")
            tasks = self.tasks(slug)
            task = next((t for t in tasks.tasks if t.number == number), None)
            if task is None:
                raise PipelineError(f"There's no task {number}. See `jef overview`.")
            if task.status is not TaskStatus.NOT_STARTED:
                raise PipelineError(f"Task {number} has already started ({task.status.value}).")
            if (slug, number) in self._in_flight:
                raise PipelineError(f"Task {number} is already being started.")
            under = None
            if on is not None:
                under = next((t for t in tasks.tasks if t.number == on), None)
                if under is None or on == number:
                    raise PipelineError(f"There's no other task {on} to build on. See `jef overview`.")
                if under.status is TaskStatus.MERGED:
                    raise PipelineError(f"Task {on} is already merged. Start task {number} without --on; it builds on main.")
                if under.status is not TaskStatus.PENDING_REVIEW:
                    raise PipelineError(
                        f"Task {on} has no open PR yet ({under.status.value}). "
                        f"Publish it first, then start task {number} on it."
                    )
            starting = [n for s, n in self._in_flight if s == slug]
            busy = [t.number for t in tasks.tasks if t.status in BUILDING] + starting
            limit = self.implementers_at_once(slug)
            if len(busy) >= limit:
                raise PipelineError(_at_limit(sorted(busy), limit))
            merged = goal_fields(self.workspace, slug).get("last merged", "")
            if under is not None:
                note = f"Started by {by} on task {on}'s branch ({pr_label(under.pr)} isn't merged yet)"
            else:
                note = f"Started by {by}" + (f" after {pr_label(merged)} merged" if merged else "")
            self._in_flight.add((slug, number))
        try:
            # git fetch and worktree add, without the lock.
            branch, base, work = self._prepare_worktree(slug, task, under)
            with self.chats.lock:
                tasks = self.tasks(slug)
                task = next((t for t in tasks.tasks if t.number == number), None)
                if task is None or task.status is not TaskStatus.NOT_STARTED:
                    raise PipelineError(f"Task {number} changed while Jef was getting it ready. See `jef overview`.")
                name = self._start_implementer(slug, tasks, task, note, branch, base, work, under)
                if under is not None:
                    self._set_stacked(slug, number, under.number)
                if problem and self.problem(slug) == problem:
                    set_goal_field(self.workspace, slug, "Problem", "none")  # it was only waiting for this
                self.write_status(slug)
        except (OSError, subprocess.SubprocessError) as err:
            raise PipelineError(f"Couldn't start task {number}: {err}") from err
        finally:
            with self.chats.lock:
                self._in_flight.discard((slug, number))
        said = f"Started {name} on task {number}"
        if under is not None:
            said += (
                f", on task {on}'s branch, so {pr_label(under.pr)} has to merge first. If task {on} changes "
                f"before it merges, have {name} rebase onto it"
            )
        return said + ". Jef wakes you when it finishes a turn."

    def send_to(self, slug: str, name: str, message: str, by: str) -> str:
        """Message one of the coordinator's agents: a nudge, feedback, a fix."""
        if not message.strip():
            raise PipelineError("The message is empty.")
        with self.chats.lock:
            project = load_project(self.workspace, slug)
            agent = self._agent(slug, name)
            if agent is None or agent.parent != project.coordinator_agent:
                raise PipelineError(f"{name!r} isn't one of your agents. See `jef overview`.")
            self.chats.send(slug, name, message.strip(), sender=by)
            return f"Sent to {name}. Jef wakes you when it finishes the turn."

    def stop_agent(self, slug: str, name: str, by: str) -> str:
        with self.chats.lock:
            project = load_project(self.workspace, slug)
            agent = self._agent(slug, name)
            if agent is None or agent.parent != project.coordinator_agent:
                raise PipelineError(f"{name!r} isn't one of your agents.")
            self.chats.stop(slug, name)
            return f"Stopped {name}."

    def wait_for_person(self, slug: str, reason: str, by: str) -> str:
        reason = reason.strip() or "no reason given"
        with self.chats.lock:
            set_goal_field(self.workspace, slug, "Problem", _short(f"{by} {WAITING}{reason}", 300))
            return "The project page now shows that you're waiting. Starting a task lifts it."

    def publish(self, slug: str, number: int, note: str = "", by: str = "you") -> str:
        """Push a task's branch and open its PR, or update the open one. The
        PR text comes from the implementer's latest ```pr block."""
        with self.chats.lock:
            tasks = self.tasks(slug)
            task = next((t for t in tasks.tasks if t.number == number), None)
            if task is None or not task.agent:
                raise PipelineError(f"Task {number} has no implementer yet.")
            if task.status is TaskStatus.MERGED:
                raise PipelineError(f"Task {number} is already merged.")
            agent = self._agent(slug, task.agent)
            if agent is None:
                raise PipelineError(f"Task {number}'s implementer isn't in agents.md.")
            if agent.status is Status.WORKING:
                raise PipelineError(f"{agent.name} is still working. Publish once its turn is done.")
            texts = [r.text for r in self.chats.replies(slug, agent.name)]
            pr = next((found for found in map(parse_pr, reversed(texts)) if found), None)
            if pr is None and not task.pr:
                raise PipelineError(
                    f"{agent.name} hasn't written a ```pr block with the PR title and description. Ask it for one."
                )
            title, body = pr if pr else (task.title, "")
            title, body = title or task.title, body or task.title
            if (slug, number) in self._in_flight:
                raise PipelineError(f"Task {number} is already being published.")
            self._in_flight.add((slug, number))
            old_pr, folder, branch = task.pr, Path(agent.folder), self._branch(slug, task)
        try:
            # Push and open the PR without the lock: a fetch or push can hang
            # for minutes, and nothing else should wait for it.
            replaced = old_pr if old_pr and self.pr_state(old_pr) == "CLOSED" else ""
            if replaced:
                # Jef only opens a new PR when the old one was closed; link it.
                body = f"Replaces {replaced}, which was closed without merging.\n\n" + body
            under = next((t for t in tasks.tasks if t.number == self.stacked(slug).get(number)), None)
            if under is not None and under.status is not TaskStatus.MERGED and under.pr:
                # It's opened against main, so until that merges it shows the other task's commits too.
                body = (
                    f"Builds on {under.pr} (task {under.number}), which isn't merged yet. "
                    "Merge that first; this PR then shows only its own changes.\n\n" + body
                )
            repo = self.folder(slug) / "repo"
            try:
                url = self.publisher.publish(folder, branch, self.git.default_branch(repo), title, body)
            except (OSError, subprocess.SubprocessError) as err:
                raise PipelineError(f"Jef couldn't publish: {err}") from err
            updated = bool(old_pr) and old_pr == url
            note_said = ""
            if updated and note.strip():
                try:
                    self.publisher.comment(folder, url, f"Jef: {_short(note, 500)}")
                    note_said = " (and left your note on it)"
                except (PublishError, OSError, subprocess.SubprocessError) as err:
                    note_said = f" (couldn't leave the note: {err})"
            with self.chats.lock:
                tasks = self.tasks(slug)
                task = next((t for t in tasks.tasks if t.number == number), None)
                if task is None:
                    raise PipelineError(f"Opened {url}, but task {number} is gone from tasks.md.")
                task.status = TaskStatus.PENDING_REVIEW
                task.pr = url
                self._save_tasks(slug, tasks)
                agent = self._agent(slug, task.agent)
                if agent is not None:
                    agent.note = ("PR updated" if updated else "PR opened") + ", waiting for your review"
                    self.chats.update(slug, agent)
                said = f"{'Updated' if updated else 'Opened'} {pr_label(url)}: {url}" + note_said
                if not updated and any(t.status is TaskStatus.NOT_STARTED for t in tasks.tasks):
                    if self.starts_tasks(slug):
                        said += (
                            ". Its slot is free: Jef starts the next task with nothing left to wait for within "
                            f"a minute. To build one on this task's code now, use `jef task start N --on {number}`."
                        )
                    else:
                        said += ". You can start the next task now, unless it needs this one's code."
                self.write_status(slug)
                return said
        finally:
            with self.chats.lock:
                self._in_flight.discard((slug, number))

    def check_prs(self, slug: str) -> str:
        """Ask GitHub about every task's PR. Records merges (stopping their
        implementers) and reports conflicts, for the coordinator to act on."""
        with self.chats.lock:
            asking = [(t.number, t.pr) for t in self.tasks(slug).tasks if t.pr and t.status is not TaskStatus.MERGED]
        # Ask GitHub without the lock; each call can take up to 30 seconds.
        answers = {}
        for number, url in asking:
            state = self.pr_state(url)
            answers[(number, url)] = (state, self.pr_mergeable(url) if state == "OPEN" else "")
        with self.chats.lock:
            tasks = self.tasks(slug)
            lines, changed = [], False
            for task in tasks.tasks:
                if (task.number, task.pr) not in answers or task.status is TaskStatus.MERGED:
                    continue
                state, mergeable = answers[(task.number, task.pr)]
                label = f"Task {task.number} ({pr_label(task.pr)})"
                if state == "MERGED":
                    self._record_merge(slug, task)
                    changed = True
                    lines.append(f"{label}: merged. Its implementer is stopped.")
                elif state == "CLOSED":
                    lines.append(f"{label}: closed without merging. Decide with the person what to do.")
                elif state == "OPEN":
                    if mergeable == "CONFLICTING":
                        lines.append(f"{label}: open, but it has merge conflicts with the main branch.")
                    elif mergeable == "MERGEABLE":
                        lines.append(f"{label}: open, waiting for the person's review. It can merge.")
                    else:
                        lines.append(f"{label}: open. GitHub is still working out whether it can merge; check again later.")
                else:
                    lines.append(f"{label}: GitHub didn't answer. Try again later.")
            if changed:
                self._save_tasks(slug, tasks)
            if tasks.tasks and all(t.status is TaskStatus.MERGED for t in tasks.tasks):
                self._finish(slug)
                lines.append("Every task is merged, so the project is done. Tell the person; Jef stops you after this turn.")
            self.write_status(slug)
            return "\n".join(lines) or "No task has a PR yet."

    def poll_merges(self, slug: str) -> list[str]:
        """Jef's own check, every few minutes while building: has any PR that's
        waiting for review been merged? Each merge is recorded as ``jef prs
        check`` would, and the coordinator is woken to start the next task.
        Only asks GitHub for the state (no tokens); conflicts and closed PRs
        stay with the coordinator. Returns the news it queued."""
        with self.chats.lock:
            if load_project(self.workspace, slug) is None or self.phase(slug) is not Phase.BUILDING:
                return []
            if self._budget_problem(slug):
                return []
            now = self.now()
            if now - self._last_pr_poll.get(slug, float("-inf")) < PR_POLL_SECONDS:
                return []
            self._last_pr_poll[slug] = now
            waiting = [(t.number, t.pr) for t in self.tasks(slug).tasks if t.status is TaskStatus.PENDING_REVIEW and t.pr]
        # Ask GitHub without the lock: each call can take up to 30 seconds,
        # and the page and the inbox mustn't wait for it.
        merged = {(number, url) for number, url in waiting if self.pr_state(url) == "MERGED"}
        if not merged:
            return []
        with self.chats.lock:
            tasks = self.tasks(slug)
            news = []
            next_step = (
                "; Jef starts the tasks that were waiting for it by itself."
                if self.starts_tasks(slug)
                else "; start the next task."
            )
            for task in tasks.tasks:
                # Re-checked: `jef prs check` may have recorded it meanwhile.
                if (task.number, task.pr) in merged and task.status is not TaskStatus.MERGED:
                    self._record_merge(slug, task)
                    stopped = f" and stopped {task.agent}" if task.agent else ""
                    news.append(
                        f"{pr_label(task.pr)} merged (task {task.number}: {task.title}). "
                        f"Jef recorded it{stopped}{next_step}"
                    )
            if not news:
                return []
            self._save_tasks(slug, tasks)
            if all(t.status is TaskStatus.MERGED for t in tasks.tasks):
                self._finish(slug)
            else:
                for item in news:
                    self._tell_coordinator(slug, item)
            self.write_status(slug)
            return news

    def _record_merge(self, slug: str, task: Task) -> None:
        """Mark a task Merged, remember its PR as the last merged, and stop
        its implementer. The caller saves the tasks."""
        task.status = TaskStatus.MERGED
        set_goal_field(self.workspace, slug, "Last merged", task.pr)
        agent = self._agent(slug, task.agent) if task.agent else None
        if agent is not None and agent.status is not Status.STOPPED:
            self.chats.stop(slug, agent.name)

    def _finish(self, slug: str) -> None:
        set_goal_field(self.workspace, slug, "Done", "yes")
        number = goal_fields(self.workspace, slug).get("backlog item", "").lstrip("#")
        backlog = Backlog.load(self.workspace.backlog_file)
        for item in backlog.items:
            if str(item.number) == number:
                item.status = BacklogStatus.DONE
        backlog.save(self.workspace.backlog_file)

    def _prepare_worktree(self, slug: str, task: Task, on: Task | None = None) -> tuple[str, str, Path]:
        """The task's branch, the main branch's name and the working folder
        (fetching first). The checkout is made from the latest main, or from
        task ``on``'s published branch when it's stacked. Network, so callers
        don't hold the lock."""
        repo = self.folder(slug) / "repo"
        main = self.git.default_branch(repo)
        # Publishing pushed task on's branch, so origin has the reviewed version.
        base = self._branch(slug, on) if on is not None else main
        branch = self._branch(slug, task)
        work = self.folder(slug) / "work" / f"impl-{task.number}"
        if not work.exists():
            self.git.new_worktree(repo, work, branch, base)
        return branch, main, work

    def _start_implementer(
        self, slug: str, tasks: TaskList, task: Task, note: str, branch: str, main: str, work: Path,
        on: Task | None = None,
    ) -> str:
        name = f"impl-{task.number}"
        project = load_project(self.workspace, slug)
        agent = self.chats.spawn(
            slug,
            name,
            Role.IMPLEMENTER,
            self._implementer_message(slug, task, branch, main, on),
            work,
            parent=project.coordinator_agent,
            instructions=skills.load("implementer"),
        )
        task.status = TaskStatus.IMPLEMENTATION
        task.agent = name
        self._save_tasks(slug, tasks)
        agent.note = note  # so you can see who started it, and why
        self.chats.update(slug, agent)
        return name

    def _branch(self, slug: str, task: Task) -> str:
        return f"jef/{slug}/task-{task.number}"

    # -- the inbox: coordinators' requests

    def process_inbox(self, slug: str) -> int:
        """Carry out waiting requests, oldest first. Returns how many. Only one
        reader per project at a time, so a request is never carried out twice
        (the heartbeat and the Check now button can both call this)."""
        with self.chats.lock:
            reader = self._inbox_locks.setdefault(slug, threading.Lock())
        if not reader.acquire(blocking=False):
            return 0
        try:
            return self._process_inbox(slug)
        finally:
            reader.release()

    def _process_inbox(self, slug: str) -> int:
        done = 0
        for request in inbox.pending(self.workspace, slug):
            try:
                ok, message = True, self._handle(slug, request)
            except (PipelineError, ChatError, PublishError) as err:
                ok, message = False, str(err)
            except Exception as err:  # never leave a coordinator waiting on a crash
                log.exception("Request %s failed in %s", request.summary, slug)
                ok, message = False, f"Jef hit an error: {err}"
            inbox.answer(self.workspace, slug, request.id, ok, message)
            done += 1
        return done

    def _handle(self, slug: str, request: inbox.Request) -> str:
        project = load_project(self.workspace, slug)
        if project is None:
            raise PipelineError("There's no such project.")
        if request.sender != project.coordinator_agent:
            raise PipelineError("Only the project's coordinator can ask Jef to do things.")
        coordinator = self._agent(slug, project.coordinator_agent)
        if coordinator is None or coordinator.status is Status.STOPPED:
            raise PipelineError("This project's coordinator has stopped.")
        by, args = project.coordinator, request.args

        def number() -> int:
            value = str(args.get("number", ""))
            if not value.isdigit():
                raise PipelineError("Give the task's number, like 2.")
            return int(value)

        def on() -> int | None:
            value = str(args.get("on", "") or "").strip()
            if not value:
                return None
            if not value.isdigit():
                raise PipelineError("Give the number of the task to build on, like --on 1.")
            return int(value)

        handlers: dict[str, Callable[[], str]] = {
            "research": lambda: self.start_research(slug, str(args.get("brief", "")), by=by),
            "plan": lambda: self.plan(slug, [str(t) for t in args.get("titles", [])], by=by),
            "approve": lambda: self.approve_tasks(slug, by=by),
            "start": lambda: self.start_task(slug, number(), by=by, on=on()),
            "send": lambda: self.send_to(slug, str(args.get("agent", "")), str(args.get("message", "")), by=by),
            "stop": lambda: self.stop_agent(slug, str(args.get("agent", "")), by=by),
            "wait": lambda: self.wait_for_person(slug, str(args.get("reason", "")), by=by),
            "publish": lambda: self.publish(slug, number(), str(args.get("note", "")), by=by),
            "prs": lambda: self.check_prs(slug),
        }
        handler = handlers.get(request.command)
        if handler is None:
            raise PipelineError(f"Jef doesn't know the request {request.command!r}.")
        return handler()

    # -- the timer

    def tick(self, slug: str) -> None:
        """Re-read the project's files and wake the coordinator if it's due."""
        with self.chats.lock:
            project = load_project(self.workspace, slug)
            if project is None:
                return
            agents = self.chats.refresh(slug)
            phase = project_phase(self.workspace, slug, agents)
            if phase is Phase.DONE:
                for agent in agents:  # each once it has finished its turn
                    if agent.status is Status.IDLE:
                        self.chats.stop(slug, agent.name)
                return
            if phase is Phase.STOPPED:
                return
            over = self._budget_problem(slug)
            if over:
                # Paused: no new turns until the budget is raised.
                if self.problem(slug) != over:
                    set_goal_field(self.workspace, slug, "Problem", over)
                return
            self._save_research(slug, agents)
            coordinator = next((a for a in agents if a.name == project.coordinator_agent), None)
            if coordinator is None or coordinator.status is not Status.IDLE:
                return  # it's busy (with you or a heartbeat); wake it later
            if phase is Phase.APPROVAL and self.auto_approve(slug) and not self.problem(slug):
                # The coordinator posted the list and finished its turn: approve
                # it, and the news below wakes the coordinator to start building.
                self.approve_tasks(slug, by="auto")
                phase = Phase.BUILDING
            now = self.now()
            last = self._last_wake.setdefault(slug, now)  # after a restart, wait a full interval
            finished = []
            for agent in agents:
                if agent.parent == coordinator.name and agent.status is Status.IDLE:
                    count = len(self.chats.replies(slug, agent.name))
                    if count > agent.handled:
                        finished.append((agent, count))
            news = self._events.get(slug, [])
            due = (
                now - last >= self.heartbeat_minutes(slug) * 60
                and not self.problem(slug)
                and (phase in (Phase.PLANNING, Phase.BUILDING) or (phase is Phase.APPROVAL and self.coordinator_decides(slug)))
            )
            if not (finished or news or due):
                return
            message = self._wake_message(slug, agents, [a for a, _ in finished], news, now - last)
            try:
                self.chats.send(slug, coordinator.name, message, sender="heartbeat")
            except ChatError as err:
                coordinator.note = "Couldn't wake it: " + _short(str(err))
                self.chats.update(slug, coordinator)
                return
            self._last_wake[slug] = now
            self._events.pop(slug, None)
            for agent, count in finished:
                agent.handled = count
                self.chats.update(slug, agent)

    def _save_research(self, slug: str, agents: list[Agent]) -> None:
        """The research agent's reply becomes research.md."""
        if goal_fields(self.workspace, slug).get("research", "").lower() != "started":
            return
        agent = next((a for a in agents if a.name == RESEARCH_AGENT), None)
        if agent is None or agent.status is Status.WORKING:
            return
        replies = [r for r in self.chats.replies(slug, agent.name) if not r.is_error and r.text.strip()]
        if not replies:
            return
        (self.folder(slug) / "research.md").write_text(replies[-1].text.strip() + "\n", encoding="utf-8")
        set_goal_field(self.workspace, slug, "Research", "done")
        agent.note = "Findings written to research.md"
        self.chats.update(slug, agent)

    # -- what agents are told

    def _task_lines(self, tasks: TaskList) -> str:
        return "\n".join(f"{t.number}. {t.title}{_after_note(t)} ({t.status.value})" for t in tasks.tasks)

    def _wake_message(self, slug: str, agents: list[Agent], finished: list[Agent], news: list[str], since: float) -> str:
        lines = [f"- {item}" for item in news]
        for agent in finished:
            if agent.name == RESEARCH_AGENT:
                lines.append("- The research agent finished. Its findings are saved as research.md; read that file.")
            else:
                lines.append(f"- {agent.name} finished a turn. Read its reply with `jef agent read {agent.name}`.")
        minutes = max(1, round(since / 60))
        head = (
            "Jef: news from your project.\n\n" + "\n".join(lines)
            if lines
            else f"Heartbeat: it's been about {minutes} minutes since your last check."
        )
        return (
            f"{head}\n\n## Project status\n\n{render_status(self.workspace, slug, agents)}\n"
            "Go through your heartbeat checklist and act with your `jef` commands. "
            "End with a short note for the person: what you did and what happens next."
        )

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

    def _research_message(self, slug: str, brief: str = "") -> str:
        project = load_project(self.workspace, slug)
        return (
            "You're the research agent for this project. Your working folder is a clone of its repo.\n\n"
            + (f"## What {project.coordinator} wants you to look into\n\n{brief.strip()}\n\n" if brief.strip() else "")
            + f"## goal.md\n\n{self._goal(slug)}\n"
            "## The conversation so far between the person and the coordinator\n\n"
            f"{self._conversation(slug)}\n\n"
            "Follow your research instructions. Your final reply becomes research.md, so make it the "
            "complete findings in the format your instructions give."
        )

    def _implementer_message(self, slug: str, task: Task, branch: str, base: str, on: Task | None = None) -> str:
        project = load_project(self.workspace, slug)
        if on is None:
            made_from = f"made from the latest `{base}`."
        else:
            under = self._branch(slug, on)
            made_from = (
                f"made from task {on.number}'s branch, `{under}`, not `{base}`. Task {on.number} "
                f"({on.title}) isn't merged yet: its PR is waiting for review. Build on its code, "
                f"but don't change it; that's task {on.number}'s job. If your coordinator says task "
                f"{on.number} changed, rebase onto `origin/{under}` (or onto `origin/{base}` once it's merged)."
            )
        return (
            f"You're the implementation agent for task {task.number}: {task.title}\n\n"
            f"Your working folder is a checkout of the repo on your own branch, `{branch}`, "
            f"{made_from} Your coordinator is {project.coordinator}.\n\n"
            f"## goal.md\n\n{self._goal(slug)}\n"
            f"## research.md\n\n{_tail(self.research(slug))}\n\n"
            "## All tasks, so you know what's yours and what isn't\n\n"
            + "\n".join(f"{t.number}. {t.title}{_after_note(t)}" for t in self.tasks(slug).tasks)
            + "\n\n"
            "When it's done and tested, commit it and end your reply with a ```pr block and "
            "`STATUS: ready`. Don't push or open the PR yourself: your coordinator reads your reply "
            "and has Jef check your commits for secrets, push your branch and open the PR. Follow "
            "your implementer instructions, and end every reply with one STATUS line."
        )


class Heartbeat:
    """Jef's timer, in a background thread while Jef runs: it carries out
    coordinators' requests every couple of seconds, and every half minute
    re-reads each project and wakes coordinators that are due. Every few
    minutes it also asks GitHub whether a PR waiting for review has merged."""

    def __init__(
        self, pipeline: Pipeline, every: float = INBOX_SECONDS, tick_every: float = TICK_SECONDS, background: bool = False
    ):
        self.pipeline = pipeline
        # With background on (as in `jef run`), each project's requests run in
        # their own thread, so a slow publish in one project doesn't hold up
        # the heartbeat or the other projects.
        self.background = background
        self._readers: dict[str, threading.Thread] = {}
        self.every = every
        self.tick_every = tick_every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="jef-heartbeat", daemon=True)
        self._last_tick = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def inbox_all(self) -> None:
        inbox.mark_alive(self.pipeline.workspace)
        for slug in self.pipeline.workspace.project_slugs():
            if not self.background:
                self._read_inbox(slug)
                continue
            reader = self._readers.get(slug)
            if reader is not None and reader.is_alive():
                continue  # still carrying out an earlier request
            if not inbox.pending(self.pipeline.workspace, slug):
                continue
            reader = threading.Thread(target=self._read_inbox, args=(slug,), name=f"jef-inbox-{slug}", daemon=True)
            self._readers[slug] = reader
            reader.start()

    def _read_inbox(self, slug: str) -> None:
        try:
            self.pipeline.process_inbox(slug)
        except Exception:
            log.exception("Couldn't read the inbox for project %s", slug)

    def tick_all(self) -> None:
        self.inbox_all()
        for slug in self.pipeline.workspace.project_slugs():
            try:
                self.pipeline.poll_merges(slug)  # before tick, so its news wakes the coordinator
            except Exception:
                log.exception("Couldn't check PRs for project %s", slug)
            try:
                self.pipeline.tick(slug)
            except Exception:  # One broken project mustn't stop the others.
                log.exception("Heartbeat failed for project %s", slug)
            try:
                # After tick, so a list it just auto-approved starts straight away.
                self.pipeline.start_ready(slug)
            except Exception:
                log.exception("Couldn't start ready tasks for project %s", slug)
            try:
                # Done and Stopped projects too, so their last status is written.
                self.pipeline.write_status(slug)
            except Exception:
                log.exception("Couldn't write status.md for project %s", slug)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            if time.monotonic() - self._last_tick >= self.tick_every:
                self._last_tick = time.monotonic()
                self.tick_all()
            else:
                self.inbox_all()
