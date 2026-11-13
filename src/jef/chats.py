"""Agents as standing chats, the KiroCrew way.

Every agent Jef spins up (a coordinator, a research agent, an implementer)
is a named Claude Code session with its own conversation:

- **Spawn** starts a new session with a fixed session ID and a first message.
- **Send** resumes that same session with another message, so the agent
  remembers everything so far. Heartbeats and "small fix" requests use this.
- No turn starts once the project's dollar budget is used up, and each turn
  is capped at what's left with Claude Code's ``--max-budget-usd``.
- **Stop** ends an agent. Stopping an agent also stops every agent it spawned,
  so closing a coordinator closes its workers.

Each turn is one ``claude -p`` run in the background. Jef keeps the record in
the project folder, in plain files:

    projects/<project>/
      agents.md            every agent: role, who spawned it, status, session
      chats/<name>.jsonl   the full conversation, one JSON event per line
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from jef import mdtable, skills
from jef.workspace import Workspace

HEADERS = ["Name", "Role", "Parent", "Status", "Handled", "Note", "Session", "Folder"]

# Agents keep their notes in Jef's project files, not in your personal Claude
# Code memory, so switch Claude Code's auto memory off for them.
AGENT_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
# Coordinators wake about once an hour, but on an API key Claude Code keeps
# its prompt cache for only 5 minutes, so every heartbeat would re-read the
# whole conversation uncached. Ask for the one-hour cache instead (Claude Code
# v2.1.242 or later; older versions ignore it). One-hour cache writes cost
# more, but each heartbeat then reads the conversation at a tenth of the price.
# A value you've set yourself is left alone.
CACHE_TTL_VAR = "CLAUDE_CODE_PROMPT_CACHE_TTL"
COORDINATOR_CACHE_TTL = "1h"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# Never allowed for any agent, whatever its role. Deny rules win over allow
# rules in Claude Code. These back up Jef's ground rules; branch protection on
# GitHub is still the real guard against pushes to main.
ALWAYS_DENIED = [
    # Jef pushes and opens PRs itself, after its secret check.
    "Bash(git push*)",
    "Bash(gh pr create*)",
    "Bash(gh pr merge *)",
    "Bash(gh api *merge*)",
    "Bash(git push * main)",
    "Bash(git push * main:*)",
    "Bash(git push * HEAD:main)",
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    # The same, with git's options before the command (git -C <folder> push).
    "Bash(git -C * push*)",
    "Bash(git -c * push*)",
    "Bash(git --git-dir* push*)",
    "Bash(git --work-tree* push*)",
]


class Role(str, Enum):
    COORDINATOR = "Coordinator"
    RESEARCH = "Research"
    IMPLEMENTER = "Implementer"
    JEF = "Jef"  # the home page's own chat


@dataclass(frozen=True)
class RolePolicy:
    """What an agent in this role may do without asking. Nobody is around to
    answer permission prompts, so anything not allowed here is refused."""

    allowed_tools: list[str]
    model: str = ""  # empty: Claude Code's default
    denied: tuple[str, ...] = ()  # refused for this role, on top of ALWAYS_DENIED
    # Only implementers change files. Claude Code's acceptEdits mode approves
    # any edit in the working folder, whatever allowed_tools says, so the
    # other roles run in the default mode and are refused file edits outright.
    can_edit: bool = False


# File-changing tools, refused for every role that isn't meant to change files.
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


# Only coordinators may run Jef's own commands; everyone else is refused them,
# even from a repo that has Jef installed in its .venv. (They'd fail anyway:
# only a coordinator's turn has JEF_PROJECT set, and Jef checks who asked.)
NO_JEF_COMMANDS = ("Bash(jef *)", "Bash(.venv/bin/jef *)")


READ_ONLY = ["Read", "Glob", "Grep"]
POLICIES: dict[Role, RolePolicy] = {
    # The coordinator runs its project through its jef commands (``jef task
    # start 1`` and so on; Jef carries each request out), and reads the repo
    # and its PRs. It can't change code.
    Role.COORDINATOR: RolePolicy(
        READ_ONLY
        + [
            "Bash(jef overview)",
            "Bash(jef research *)",
            "Bash(jef tasks *)",
            "Bash(jef task *)",
            "Bash(jef prs *)",
            "Bash(jef agent *)",
            "Bash(jef wait *)",
            "Bash(gh pr view *)",
            "Bash(gh pr list *)",
            "Bash(gh pr diff *)",
            "Bash(gh pr checks *)",
            "Bash(git log *)",
            "Bash(git show *)",
            "Bash(git diff *)",
            "Bash(git fetch *)",
            "Bash(git fetch)",
            "Bash(git status)",
        ]
    ),
    Role.RESEARCH: RolePolicy(READ_ONLY + ["WebSearch", "WebFetch", "Bash(git log *)"], denied=NO_JEF_COMMANDS),
    # Jef's own chat isn't in a repo: it only reads the workspace files.
    Role.JEF: RolePolicy(READ_ONLY, denied=NO_JEF_COMMANDS),
    Role.IMPLEMENTER: RolePolicy(
        READ_ONLY
        + [
            "Bash(git *)",
            "Bash(gh pr view *)",
            "Bash(gh pr list *)",
            # Setting up and testing the project.
            "Bash(python *)",
            "Bash(python3 *)",
            "Bash(pip *)",
            "Bash(pip3 *)",
            "Bash(uv *)",
            "Bash(pytest *)",
            "Bash(.venv/bin/*)",
            "Bash(npm *)",
            "Bash(npx *)",
            "Bash(pnpm *)",
            "Bash(yarn *)",
            "Bash(make *)",
            "Bash(cargo *)",
            "Bash(go *)",
            "Bash(test *)",
            "Bash(ls *)",
            "Bash(mkdir *)",
        ],
        denied=NO_JEF_COMMANDS,
        can_edit=True,
    ),
}

# Each role's skill, sent with every turn. Claude Code records the system
# prompt on a conversation's first turn and reuses it on --resume, so this is
# part of the cached prefix either way; sending it again is harmless and puts
# it back after Claude Code compacts the conversation.
SKILLS = {Role.COORDINATOR: "coordinator", Role.RESEARCH: "research", Role.IMPLEMENTER: "implementer", Role.JEF: "jef"}


def jef_bin_dir() -> str:
    """The folder holding the ``jef`` command, so coordinators can run it."""
    here = Path(sys.executable).parent
    if (here / "jef").exists():
        return str(here)
    found = shutil.which("jef")
    return str(Path(found).parent) if found else ""


class Status(str, Enum):
    IDLE = "Idle"  # finished its last turn, waiting for a message
    WORKING = "Working"  # a turn is running right now
    STOPPED = "Stopped"  # ended for good


@dataclass
class Agent:
    name: str
    role: Role
    session: str
    folder: str
    parent: str = ""
    status: Status = Status.IDLE
    handled: int = 0  # how many finished turns the heartbeat has acted on
    note: str = ""  # the latest thing worth showing, like "PR opened" or why it's stuck


@dataclass
class ChatLine:
    kind: str  # "sent", "text", "tool", "result" or "error"
    text: str


@dataclass
class Reply:
    text: str
    is_error: bool = False
    subtype: str = ""  # Claude Code's result subtype, like "error_max_budget_usd"


class ChatError(RuntimeError):
    pass


BUDGET_USED_UP = "This project's budget is used up. Raise the budget on the project page to keep going."


def _roster_path(ws: Workspace, project: str) -> Path:
    return ws.project_dir(project) / "agents.md"


def load_roster(ws: Workspace, project: str) -> list[Agent]:
    path = _roster_path(ws, project)
    if not path.exists():
        return []
    doc = mdtable.parse(path.read_text(encoding="utf-8"))
    col = {h.lower(): i for i, h in enumerate(doc.headers)}
    agents = []
    for row in doc.rows:
        get = lambda name: row[col[name]] if name in col else ""  # noqa: E731
        try:
            agents.append(
                Agent(
                    name=get("name"),
                    role=Role(get("role")),
                    session=get("session"),
                    folder=get("folder"),
                    parent=get("parent"),
                    status=Status(get("status") or Status.IDLE.value),
                    handled=int(get("handled")) if get("handled").isdigit() else 0,
                    note=get("note"),
                )
            )
        except ValueError:
            continue  # A row someone hand-edited into nonsense; skip it.
    return agents


def save_roster(ws: Workspace, project: str, agents: list[Agent]) -> None:
    path = _roster_path(ws, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = mdtable.parse(path.read_text(encoding="utf-8")) if path.exists() else mdtable.TableDoc()
    if not doc.headers:
        doc.before = [f"# Agents: {project}", ""]
    doc.headers = HEADERS
    doc.rows = [
        [a.name, a.role.value, a.parent, a.status.value, str(a.handled), a.note, a.session, a.folder] for a in agents
    ]
    path.write_text(mdtable.render(doc), encoding="utf-8")


@dataclass
class Chats:
    """Spawns agents as Claude Code sessions and passes messages to them."""

    workspace: Workspace
    claude_path: str
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    new_session_id: Callable[[], str] = lambda: str(uuid.uuid4())
    # Dollars left in a project's budget, or None for no budget. Passed in
    # (see ``usage.budget_left``) so this module doesn't import the projects code.
    budget_left: Callable[[str], float | None] = lambda project: None
    # The model for an agent's turn, or "" for the role's own (see
    # ``models.model_for``). Looked up on every turn, so a change applies next turn.
    model_for: Callable[[str, Role], str] = lambda project, role: ""
    _running: dict[tuple[str, str], subprocess.Popen] = field(default_factory=dict)
    # The web pages and the heartbeat both change agents.md; one at a time.
    lock: threading.RLock = field(default_factory=threading.RLock)

    # ---- files

    def chat_file(self, project: str, name: str) -> Path:
        return self.workspace.project_dir(project) / "chats" / f"{name}.jsonl"

    def _find(self, agents: list[Agent], name: str) -> Agent:
        for agent in agents:
            if agent.name == name:
                return agent
        raise ChatError(f"There's no agent named {name!r} in this project.")

    # ---- commands

    def _check_budget(self, project: str) -> float | None:
        """Refuse to start a turn once the budget is used up. Returns what's left."""
        left = self.budget_left(project)
        if left is not None and left <= 0:
            raise ChatError(BUDGET_USED_UP)
        return left

    def command(
        self, agent: Agent, project: str, first_turn: bool, instructions: str = "", budget_left: float | None = None
    ) -> list[str]:
        policy = POLICIES[agent.role]
        cmd = [self.claude_path, "-p", "--output-format", "stream-json", "--verbose"]
        if first_turn:
            cmd += ["--session-id", agent.session, "--name", f"jef · {project} · {agent.name}"]
        else:
            cmd += ["--resume", agent.session]
        mode = "acceptEdits" if policy.can_edit else "default"
        cmd += ["--permission-mode", mode, "--permission-prompts", "none"]
        cmd += ["--allowedTools", *policy.allowed_tools]
        denied = [*ALWAYS_DENIED, *policy.denied, *(() if policy.can_edit else EDIT_TOOLS)]
        cmd += ["--disallowedTools", *denied]
        model = self.model_for(project, agent.role) or policy.model
        if model:
            cmd += ["--model", model]
        if budget_left is not None:
            # Claude Code stops this one run once it has spent what's left. It
            # doesn't count earlier runs, which is why Jef passes the remainder.
            cmd += ["--max-budget-usd", f"{max(budget_left, 0.01):.2f}"]
        instructions = instructions or skills.load(SKILLS[agent.role])
        if instructions:
            cmd += ["--append-system-prompt", instructions]
        return cmd

    def agent_env(self, project: str, agent: Agent) -> dict[str, str]:
        """The environment an agent's turn runs in."""
        env = {**os.environ, **AGENT_ENV}
        for key in ("JEF_PROJECT", "JEF_AGENT"):
            env.pop(key, None)  # only coordinators get these
        if agent.role is Role.COORDINATOR:
            # The coordinator's jef commands read these to know whose request it is.
            env["JEF_HOME"] = str(self.workspace.root)
            env["JEF_PROJECT"] = project
            env["JEF_AGENT"] = agent.name
            env.setdefault(CACHE_TTL_VAR, COORDINATOR_CACHE_TTL)
            bin_dir = jef_bin_dir()
            if bin_dir:
                env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        return env

    def _run_turn(
        self,
        project: str,
        agent: Agent,
        message: str,
        sender: str,
        first_turn: bool,
        instructions: str = "",
        budget_left: float | None = None,
    ) -> None:
        log = self.chat_file(project, agent.name)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"type": "jef_sent", "from": sender, "text": message}) + "\n")
        # The message goes in on stdin (from a file, so a long one can't block).
        prompt_file = log.with_suffix(".prompt")
        prompt_file.write_text(message, encoding="utf-8")
        with prompt_file.open("rb") as stdin, log.open("ab") as stdout, log.with_suffix(".err.log").open("ab") as stderr:
            proc = self.popen(
                self.command(agent, project, first_turn, instructions, budget_left),
                cwd=agent.folder,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env=self.agent_env(project, agent),
                start_new_session=True,  # so stopping Jef's terminal doesn't kill agents mid-turn
            )
        self._running[(project, agent.name)] = proc

    def spawn(
        self,
        project: str,
        name: str,
        role: Role,
        message: str,
        folder: str | Path,
        parent: str = "",
        instructions: str = "",
        sender: str = "",
    ) -> Agent:
        """Start a new agent with its first message. ``sender`` is who it's
        from; by default the parent, or "you"."""
        with self.lock:
            return self._spawn(project, name, role, message, folder, parent, instructions, sender)

    def _spawn(self, project, name, role, message, folder, parent, instructions, sender="") -> Agent:
        if not NAME_PATTERN.match(name):
            raise ChatError("Agent names use lowercase letters, numbers and dashes, like impl-2.")
        if not Path(folder).is_dir():
            raise ChatError(f"The folder {folder} doesn't exist.")
        agents = self.refresh(project)
        if any(a.name == name for a in agents):
            raise ChatError(f"There's already an agent named {name!r} in this project.")
        if parent:
            self._find(agents, parent)
        left = self._check_budget(project)
        agent = Agent(name, role, self.new_session_id(), str(Path(folder).resolve()), parent, Status.WORKING)
        agents.append(agent)
        save_roster(self.workspace, project, agents)
        self._run_turn(
            project, agent, message, sender=sender or parent or "you", first_turn=True, instructions=instructions, budget_left=left
        )
        return agent

    def send(self, project: str, name: str, message: str, sender: str = "you", instructions: str = "") -> Agent:
        """Send another message to an agent, in the same conversation."""
        with self.lock:
            return self._send(project, name, message, sender, instructions)

    def _send(self, project, name, message, sender, instructions) -> Agent:
        agents = self.refresh(project)
        agent = self._find(agents, name)
        if agent.status is Status.STOPPED:
            raise ChatError(f"{name} has been stopped.")
        if agent.status is Status.WORKING:
            raise ChatError(f"{name} is still working on its last message. Try again when it's done.")
        left = self._check_budget(project)
        agent.status = Status.WORKING
        save_roster(self.workspace, project, agents)
        self._run_turn(project, agent, message, sender, first_turn=False, instructions=instructions, budget_left=left)
        return agent

    def refresh(self, project: str) -> list[Agent]:
        """Update statuses: an agent whose turn has ended is Idle again."""
        with self.lock:
            return self._refresh(project)

    def _refresh(self, project: str) -> list[Agent]:
        agents = load_roster(self.workspace, project)
        changed = False
        for agent in agents:
            if agent.status is not Status.WORKING:
                continue
            proc = self._running.get((project, agent.name))
            # No process means Jef restarted mid-turn; that turn is over.
            if proc is None or proc.poll() is not None:
                self._running.pop((project, agent.name), None)
                agent.status = Status.IDLE
                changed = True
        if changed:
            save_roster(self.workspace, project, agents)
        return agents

    def stop(self, project: str, name: str) -> list[str]:
        """Stop an agent and every agent it spawned. Returns the names stopped."""
        with self.lock:
            return self._stop(project, name)

    def _stop(self, project: str, name: str) -> list[str]:
        agents = self.refresh(project)
        self._find(agents, name)
        to_stop, queue = [], [name]
        while queue:
            current = queue.pop()
            to_stop.append(current)
            queue += [a.name for a in agents if a.parent == current and a.name not in to_stop]
        for agent in agents:
            if agent.name in to_stop:
                proc = self._running.pop((project, agent.name), None)
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                agent.status = Status.STOPPED
        save_roster(self.workspace, project, agents)
        return to_stop

    def stop_project(self, project: str) -> None:
        """End every running turn in a project that is being deleted, and
        forget them. agents.md isn't touched: it goes with the folder."""
        with self.lock:
            for key in [k for k in self._running if k[0] == project]:
                proc = self._running.pop(key)
                if proc.poll() is None:
                    proc.terminate()

    def stop_running_turns(self) -> None:
        """When Jef shuts down, end the turns still running. Their agents show
        as Idle next time, and the heartbeat picks them up again."""
        with self.lock:
            for proc in self._running.values():
                if proc.poll() is None:
                    proc.terminate()
            self._running.clear()

    def update(self, project: str, agent: Agent) -> None:
        """Save an agent's handled count and note."""
        with self.lock:
            agents = load_roster(self.workspace, project)
            for i, existing in enumerate(agents):
                if existing.name == agent.name:
                    existing.handled, existing.note = agent.handled, agent.note
            save_roster(self.workspace, project, agents)

    # ---- reading conversations

    def replies(self, project: str, name: str) -> list[Reply]:
        """The final reply of every finished turn, oldest first."""
        path = self.chat_file(project, name)
        if not path.exists():
            return []
        out = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                out.append(
                    Reply(str(event.get("result") or ""), bool(event.get("is_error")), str(event.get("subtype") or ""))
                )
        return out

    def transcript(self, project: str, name: str) -> list[ChatLine]:
        path = self.chat_file(project, name)
        if not path.exists():
            return []
        lines: list[ChatLine] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            lines.extend(_describe(event))
        return lines


def _describe(event: dict) -> list[ChatLine]:
    kind = event.get("type")
    if kind == "jef_sent":
        return [ChatLine("sent", f"{event.get('from', 'you')}: {event.get('text', '')}")]
    if kind == "assistant":
        out = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append(ChatLine("text", block["text"].strip()))
            elif block.get("type") == "tool_use":
                detail = block.get("input", {})
                summary = detail.get("command") or detail.get("file_path") or detail.get("pattern") or ""
                out.append(ChatLine("tool", f"{block.get('name')} {summary}".strip()))
        return out
    if kind == "result":
        if event.get("is_error"):
            if "budget" in str(event.get("subtype") or ""):
                return [ChatLine("error", "Stopped partway: this turn used up the rest of the project's budget.")]
            return [ChatLine("error", str(event.get("result") or event.get("subtype") or "The turn failed."))]
        return [ChatLine("result", "Turn finished.")]
    return []
