"""How a project runs, from "go" to merged PRs.

**The coordinator runs its project.** It talks with you, starts research,
plans the tasks, starts implementers one at a time, nudges them, checks their
PRs on GitHub (merged? conflicts?) and sends work back. It does all of that
with ``jef project …`` commands (see ``jef.coordinate``), which Jef carries out
from its inbox (``jef.inbox``). Jef doesn't decide anything itself.

**Jef only keeps time and keeps the rules.** While it runs, it:

- carries out coordinators' requests within a few seconds, refusing anything
  against the rules (one task at a time, only the coordinator's own agents,
  secrets never pushed, never ``main``),
- wakes each coordinator once per heartbeat (an hour by default), or sooner
  when one of its agents finishes a turn or you press a button,
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

from jef import inbox, skills, usage
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Agent, Chats, ChatError, Role, Status, load_roster
from jef.projects import DEFAULT_HEARTBEAT_MINUTES, goal_fields, load_project, set_goal_field
from jef.publish import Publisher, PublishError
from jef.status import WAITING, Phase, project_phase, render_status, write_status
from jef.tasks import Task, TaskList, TaskStatus
from jef.workspace import Workspace

log = logging.getLogger("jef.pipeline")

RESEARCH_AGENT = "research"
INBOX_SECONDS = 2  # how often Jef looks for coordinators' requests
TICK_SECONDS = 30  # how often Jef re-reads the files and sees who to wake
# Tasks usually build on each other, so one task is in flight at a time: the
# coordinator starts the next once the last one's PR is merged.
IMPLEMENTERS_AT_ONCE = 1
MAX_CONTEXT_CHARS = 30_000
MAX_READ_CHARS = 12_000

PR_BLOCK = re.compile(r"```pr\s*\n(.*?)```", re.S | re.I)
BUDGET_PROBLEM = "Budget reached"  # how a budget pause starts, so raising the budget can lift it
MAX_BUDGET_USD = 100_000
IN_FLIGHT = (TaskStatus.IMPLEMENTATION, TaskStatus.PENDING_REVIEW)


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


# ---- reading agents' replies (also used by ``jef project read``)


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

    def coordinator_decides(self, slug: str) -> bool:
        """Whether the coordinator may approve its own task list once it has
        waited a heartbeat for you. On unless goal.md says ``Coordinator decides: no``."""
        return goal_fields(self.workspace, slug).get("coordinator decides", "").lower() != "no"

    def set_coordinator_decides(self, slug: str, on: bool) -> None:
        set_goal_field(self.workspace, slug, "Coordinator decides", "yes" if on else "no")

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
        titles = [_short(t, 200) for t in titles if t.strip()]
        with self.chats.lock:
            phase = self.phase(slug)
            if phase in (Phase.UNDERSTANDING, Phase.RESEARCH):
                raise PipelineError("Research hasn't finished yet, so there's nothing to plan from.")
            if phase in (Phase.DONE, Phase.STOPPED):
                raise PipelineError("This project is finished.")
            if not titles and phase is not Phase.BUILDING:
                raise PipelineError('Give at least one task, like: jef project plan "Show the version in the footer"')
            tasks = self.tasks(slug)
            if phase is Phase.BUILDING:
                kept = [t for t in tasks.tasks if t.status is not TaskStatus.NOT_STARTED]
                after = max((t.number for t in kept), default=0)
                tasks.tasks = kept + [Task(after + i, title) for i, title in enumerate(titles, start=1)]
                self._save_tasks(slug, tasks)
                self.write_status(slug)
                return "Kept the tasks that have started, and set the rest:\n" + self._task_lines(tasks)
            tasks.tasks = [Task(i, title) for i, title in enumerate(titles, start=1)]
            self._save_tasks(slug, tasks)
            # The list waits for the person: the coordinator may approve it
            # only once it has waited a whole heartbeat.
            set_goal_field(self.workspace, slug, "Tasks proposed", str(int(self.now())))
            self.write_status(slug)
            return "Saved the task list. It now waits for the person's approval:\n" + self._task_lines(tasks)

    def approve_tasks(self, slug: str, by: str = "you") -> str:
        with self.chats.lock:
            if self.phase(slug) is not Phase.APPROVAL:
                raise PipelineError("There are no tasks waiting for approval.")
            if by != "you":
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
            if by == "you":
                self._tell_coordinator(slug, "The person approved the tasks. Start the first one with `jef project start 1`.")
            self.write_status(slug)
            return "The tasks are approved. Start the first with `jef project start 1`."

    def start_task(self, slug: str, number: int, by: str) -> str:
        with self.chats.lock:
            if self.phase(slug) is not Phase.BUILDING:
                raise PipelineError("The tasks haven't been approved yet.")
            problem = self.problem(slug)
            if problem and WAITING not in problem:
                raise PipelineError(f"There's a problem to sort out first: {problem}")
            tasks = self.tasks(slug)
            task = next((t for t in tasks.tasks if t.number == number), None)
            if task is None:
                raise PipelineError(f"There's no task {number}. See `jef project status`.")
            if task.status is not TaskStatus.NOT_STARTED:
                raise PipelineError(f"Task {number} has already started ({task.status.value}).")
            busy = [t for t in tasks.tasks if t.status in IN_FLIGHT]
            if len(busy) >= IMPLEMENTERS_AT_ONCE:
                raise PipelineError(
                    f"Task {busy[0].number} isn't merged yet, and only {IMPLEMENTERS_AT_ONCE} task is built at a time."
                )
            merged = goal_fields(self.workspace, slug).get("last merged", "")
            note = f"Started by {by}" + (f" after {pr_label(merged)} merged" if merged else "")
            try:
                name = self._start_implementer(slug, tasks, task, note)
            except (OSError, subprocess.SubprocessError) as err:
                raise PipelineError(f"Couldn't start task {number}: {err}") from err
            if problem:
                set_goal_field(self.workspace, slug, "Problem", "none")  # it was only waiting for this
            self.write_status(slug)
            return f"Started {name} on task {number}. Jef wakes you when it finishes a turn."

    def send_to(self, slug: str, name: str, message: str, by: str) -> str:
        """Message one of the coordinator's agents: a nudge, feedback, a fix."""
        if not message.strip():
            raise PipelineError("The message is empty.")
        with self.chats.lock:
            project = load_project(self.workspace, slug)
            agent = self._agent(slug, name)
            if agent is None or agent.parent != project.coordinator_agent:
                raise PipelineError(f"{name!r} isn't one of your agents. See `jef project status`.")
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
            replaced = task.pr if task.pr and self.pr_state(task.pr) == "CLOSED" else ""
            if replaced:
                # Jef only opens a new PR when the old one was closed; link it.
                body = f"Replaces {replaced}, which was closed without merging.\n\n" + body
            repo = self.folder(slug) / "repo"
            try:
                url = self.publisher.publish(
                    Path(agent.folder), self._branch(slug, task), self.git.default_branch(repo), title, body
                )
            except (OSError, subprocess.SubprocessError) as err:
                raise PipelineError(f"Jef couldn't publish: {err}") from err
            updated = bool(task.pr) and task.pr == url
            task.status = TaskStatus.PENDING_REVIEW
            task.pr = url
            self._save_tasks(slug, tasks)
            agent.note = ("PR updated" if updated else "PR opened") + ", waiting for your review"
            self.chats.update(slug, agent)
            said = f"{'Updated' if updated else 'Opened'} {pr_label(url)}: {url}"
            if updated and note.strip():
                try:
                    self.publisher.comment(Path(agent.folder), url, f"Jef: {_short(note, 500)}")
                    said += " (and left your note on it)"
                except (PublishError, OSError, subprocess.SubprocessError) as err:
                    said += f" (couldn't leave the note: {err})"
            self.write_status(slug)
            return said

    def check_prs(self, slug: str) -> str:
        """Ask GitHub about every task's PR. Records merges (stopping their
        implementers) and reports conflicts, for the coordinator to act on."""
        with self.chats.lock:
            tasks = self.tasks(slug)
            lines, changed = [], False
            for task in tasks.tasks:
                if not task.pr or task.status is TaskStatus.MERGED:
                    continue
                state = self.pr_state(task.pr)
                label = f"Task {task.number} ({pr_label(task.pr)})"
                if state == "MERGED":
                    task.status, changed = TaskStatus.MERGED, True
                    set_goal_field(self.workspace, slug, "Last merged", task.pr)
                    agent = self._agent(slug, task.agent) if task.agent else None
                    if agent is not None and agent.status is not Status.STOPPED:
                        self.chats.stop(slug, agent.name)
                    lines.append(f"{label}: merged. Its implementer is stopped.")
                elif state == "CLOSED":
                    lines.append(f"{label}: closed without merging. Decide with the person what to do.")
                elif state == "OPEN":
                    mergeable = self.pr_mergeable(task.pr)
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

    def _finish(self, slug: str) -> None:
        set_goal_field(self.workspace, slug, "Done", "yes")
        number = goal_fields(self.workspace, slug).get("backlog item", "").lstrip("#")
        backlog = Backlog.load(self.workspace.backlog_file)
        for item in backlog.items:
            if str(item.number) == number:
                item.status = BacklogStatus.DONE
        backlog.save(self.workspace.backlog_file)

    def _start_implementer(self, slug: str, tasks: TaskList, task: Task, note: str) -> str:
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
        self._save_tasks(slug, tasks)
        agent.note = note  # so you can see who started it, and why
        self.chats.update(slug, agent)
        return name

    def _branch(self, slug: str, task: Task) -> str:
        return f"jef/{slug}/task-{task.number}"

    # -- the inbox: coordinators' ``jef project …`` requests

    def process_inbox(self, slug: str) -> int:
        """Carry out waiting requests, oldest first. Returns how many."""
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

        handlers: dict[str, Callable[[], str]] = {
            "research": lambda: self.start_research(slug, str(args.get("brief", "")), by=by),
            "plan": lambda: self.plan(slug, [str(t) for t in args.get("titles", [])], by=by),
            "approve": lambda: self.approve_tasks(slug, by=by),
            "start": lambda: self.start_task(slug, number(), by=by),
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
        return "\n".join(f"{t.number}. {t.title} ({t.status.value})" for t in tasks.tasks)

    def _wake_message(self, slug: str, agents: list[Agent], finished: list[Agent], news: list[str], since: float) -> str:
        lines = [f"- {item}" for item in news]
        for agent in finished:
            if agent.name == RESEARCH_AGENT:
                lines.append("- The research agent finished. Its findings are saved as research.md; read that file.")
            else:
                lines.append(f"- {agent.name} finished a turn. Read its reply with `jef project read {agent.name}`.")
        minutes = max(1, round(since / 60))
        head = (
            "Jef: news from your project.\n\n" + "\n".join(lines)
            if lines
            else f"Heartbeat: it's been about {minutes} minutes since your last check."
        )
        return (
            f"{head}\n\n## Project status\n\n{render_status(self.workspace, slug, agents)}\n"
            "Go through your heartbeat checklist and act with `jef project` commands. "
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

    def _implementer_message(self, slug: str, task: Task, branch: str, base: str) -> str:
        project = load_project(self.workspace, slug)
        return (
            f"You're the implementation agent for task {task.number}: {task.title}\n\n"
            f"Your working folder is a checkout of the repo on your own branch, `{branch}`, "
            f"made from the latest `{base}`. Your coordinator is {project.coordinator}.\n\n"
            f"## goal.md\n\n{self._goal(slug)}\n"
            f"## research.md\n\n{_tail(self.research(slug))}\n\n"
            "## All tasks, so you know what's yours and what isn't\n\n"
            + "\n".join(f"{t.number}. {t.title}" for t in self.tasks(slug).tasks)
            + "\n\n"
            "When it's done and tested, commit it and end your reply with a ```pr block and "
            "`STATUS: ready`. Don't push or open the PR yourself: your coordinator reads your reply "
            "and has Jef check your commits for secrets, push your branch and open the PR. Follow "
            "your implementer instructions, and end every reply with one STATUS line."
        )


class Heartbeat:
    """Jef's timer, in a background thread while Jef runs: it carries out
    coordinators' requests every couple of seconds, and every half minute
    re-reads each project and wakes coordinators that are due."""

    def __init__(self, pipeline: Pipeline, every: float = INBOX_SECONDS, tick_every: float = TICK_SECONDS):
        self.pipeline = pipeline
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
            try:
                self.pipeline.process_inbox(slug)
            except Exception:
                log.exception("Couldn't read the inbox for project %s", slug)

    def tick_all(self) -> None:
        self.inbox_all()
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
            if time.monotonic() - self._last_tick >= self.tick_every:
                self._last_tick = time.monotonic()
                self.tick_all()
            else:
                self.inbox_all()
