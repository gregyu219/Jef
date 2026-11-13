"""The commands a coordinator runs its project with.

Only a coordinator's turn can use them: Jef sets ``JEF_PROJECT`` and
``JEF_AGENT`` for coordinators and nobody else, and only coordinators are
allowed to run them. Reading (``overview``, ``agent read``) happens right
here. Anything that changes the project goes to Jef's inbox, and the running
Jef carries it out and answers within a few seconds, so its rules always apply.

    jef overview                      the project at a glance
    jef research start ["brief"]      start the research agent
    jef tasks set "Task" "Task" …     set the task list
    jef tasks approve                 approve the list (after it waited a heartbeat)
    jef task start 1                  start task 1's implementer (it's named impl-1)
    jef task start 2 --on 1           start task 2 on task 1's branch (its PR is open, not merged)
    jef task publish 1 [--note …]     push task 1's branch and open or update its PR
    jef prs check                     ask GitHub about every PR (merged? conflicts?)
    jef agent read impl-1             an agent's latest reply
    jef agent send impl-1 "…"         message one of your agents
    jef agent stop impl-1             stop one of your agents
    jef wait "reason"                 show the person you're waiting for them
"""

from __future__ import annotations

import argparse
import os

from jef import inbox
from jef.chats import load_roster
from jef.projects import load_project
from jef.status import render_status
from jef.workspace import Workspace

ANSWER_SECONDS = 100  # under the two minutes Claude Code gives a command

NOT_A_COORDINATOR = (
    "This command is for Jef's coordinators: it only works inside a coordinator's turn, "
    "where Jef has set JEF_PROJECT and JEF_AGENT."
)
JEF_NOT_RUNNING = (
    "Jef isn't running, so it can't do that right now. Tell the person to start Jef (`jef start`), "
    "then try again."
)


# The top-level words these commands start with, for the CLI and for the
# coordinator's allowed tools.
COMMAND_WORDS = ("overview", "research", "tasks", "task", "prs", "agent", "wait")


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Add the coordinator's commands to the ``jef`` command."""

    def group(name: str, help: str) -> argparse._SubParsersAction:
        parser = sub.add_parser(name, help=f"(for coordinators) {help}")
        parser.set_defaults(coordinator=True)
        return parser.add_subparsers(dest="action", metavar="action", required=True)

    overview = sub.add_parser("overview", help="(for coordinators) the project at a glance")
    overview.set_defaults(coordinator=True, action="overview")

    group("research", "the research agent").add_parser("start", help="start the research agent").add_argument(
        "brief", nargs="*", help="what to look into (optional)"
    )
    tasks = group("tasks", "the task list")
    tasks.add_parser("set", help="set the task list, one quoted title per task").add_argument("titles", nargs="*")
    tasks.add_parser("approve", help="approve the task list")

    task = group("task", "one task")
    start = task.add_parser("start", help="start the task's implementer")
    start.add_argument("number")
    start.add_argument("--on", default="", help="build on this unmerged task's branch instead of main")
    publish = task.add_parser("publish", help="push the task's branch and open or update its PR")
    publish.add_argument("number")
    publish.add_argument("--note", default="", help="when updating a PR, a comment saying why")

    group("prs", "pull requests").add_parser("check", help="ask GitHub about every PR")

    agent = group("agent", "your agents")
    agent.add_parser("read", help="an agent's latest reply").add_argument("agent")
    send = agent.add_parser("send", help="message one of your agents")
    send.add_argument("agent")
    send.add_argument("message", nargs="+")
    agent.add_parser("stop", help="stop one of your agents").add_argument("agent")

    wait = sub.add_parser("wait", help="(for coordinators) show the person you're waiting for them")
    wait.set_defaults(coordinator=True, action="wait")
    wait.add_argument("reason", nargs="+")


def request_args(args: argparse.Namespace) -> tuple[str, dict]:
    """The inbox request for a command that changes the project."""
    word, action = args.command, args.action
    if word == "research":
        return "research", {"brief": " ".join(args.brief)}
    if word == "tasks":
        return ("plan", {"titles": list(args.titles)}) if action == "set" else ("approve", {})
    if word == "task":
        extra = {"note": args.note} if action == "publish" else {"on": args.on} if args.on else {}
        return action, {"number": args.number, **extra}  # start, publish
    if word == "prs":
        return "prs", {}
    if word == "agent":
        if action == "send":
            return "send", {"agent": args.agent, "message": " ".join(args.message)}
        return "stop", {"agent": args.agent}
    if word == "wait":
        return "wait", {"reason": " ".join(args.reason)}
    raise ValueError(f"not a changing command: {word} {action}")


def run(args: argparse.Namespace, ws: Workspace | None = None, env: dict | None = None,
        wait_seconds: float = ANSWER_SECONDS) -> int:
    env = os.environ if env is None else env
    slug, sender = env.get("JEF_PROJECT", ""), env.get("JEF_AGENT", "")
    ws = ws or Workspace.default()
    project = load_project(ws, slug) if slug else None
    if not slug or not sender or project is None or project.coordinator_agent != sender:
        print(NOT_A_COORDINATOR)
        return 2

    if args.command == "overview":
        print(render_status(ws, slug, load_roster(ws, slug)))
        return 0
    if args.command == "agent" and args.action == "read":
        from jef.pipeline import PipelineError, last_reply

        try:
            print(last_reply(ws, slug, args.agent))
        except PipelineError as err:
            print(err)
            return 1
        return 0

    if not inbox.jef_is_running(ws):
        print(JEF_NOT_RUNNING)
        return 1
    command, payload = request_args(args)
    request_id = inbox.submit(ws, slug, sender, command, payload)
    answer = inbox.wait_for_answer(ws, slug, request_id, wait_seconds)
    if answer is None:
        print("Jef is still working on that. Check with `jef overview` in a little while.")
        return 1
    print(answer.get("message", ""))
    return 0 if answer.get("ok") else 1
