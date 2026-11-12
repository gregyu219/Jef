"""``jef project …``: the commands a coordinator runs its project with.

Only a coordinator's turn can use them: Jef sets ``JEF_PROJECT`` and
``JEF_AGENT`` for coordinators and nobody else, and only coordinators are
allowed to run ``jef``. Reading (``status``, ``read``) happens right here.
Anything that changes the project goes to Jef's inbox, and the running Jef
carries it out and answers within a few seconds, so its rules always apply.

    jef project status                 the project at a glance
    jef project read impl-2            an agent's latest reply
    jef project research ["brief"]     start the research agent
    jef project plan "Task" "Task" …   set the task list
    jef project approve                approve the list (after it waited a heartbeat)
    jef project start 2                start task 2's implementer
    jef project send impl-2 "…"        message one of your agents
    jef project publish 2 [--note …]   push task 2's branch and open or update its PR
    jef project prs                    ask GitHub about every PR (merged? conflicts?)
    jef project wait "reason"          show the person you're waiting for them
    jef project stop impl-2            stop one of your agents
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
    "`jef project` is for Jef's coordinators: it only works inside a coordinator's turn, "
    "where Jef has set JEF_PROJECT and JEF_AGENT."
)
JEF_NOT_RUNNING = (
    "Jef isn't running, so it can't do that right now. Tell the person to start Jef (`jef start`), "
    "then try again."
)


def add_parser(sub: argparse._SubParsersAction) -> None:
    project = sub.add_parser("project", help="(for coordinators) run a project")
    cmds = project.add_subparsers(dest="action", metavar="action", required=True)
    cmds.add_parser("status", help="the project at a glance")
    read = cmds.add_parser("read", help="an agent's latest reply")
    read.add_argument("agent")
    research = cmds.add_parser("research", help="start the research agent")
    research.add_argument("brief", nargs="*", help="what to look into (optional)")
    plan = cmds.add_parser("plan", help="set the task list, one quoted title per task")
    plan.add_argument("titles", nargs="*")
    cmds.add_parser("approve", help="approve the task list")
    start = cmds.add_parser("start", help="start a task's implementer")
    start.add_argument("number")
    send = cmds.add_parser("send", help="message one of your agents")
    send.add_argument("agent")
    send.add_argument("message", nargs="+")
    stop = cmds.add_parser("stop", help="stop one of your agents")
    stop.add_argument("agent")
    wait = cmds.add_parser("wait", help="show the person you're waiting for them")
    wait.add_argument("reason", nargs="+")
    publish = cmds.add_parser("publish", help="push a task's branch and open or update its PR")
    publish.add_argument("number")
    publish.add_argument("--note", default="", help="when updating a PR, a comment saying why")
    cmds.add_parser("prs", help="ask GitHub about every PR")


def request_args(args: argparse.Namespace) -> tuple[str, dict]:
    """The inbox request for a changing command."""
    action = args.action
    if action == "research":
        return action, {"brief": " ".join(args.brief)}
    if action == "plan":
        return action, {"titles": list(args.titles)}
    if action in ("start", "publish"):
        extra = {"note": args.note} if action == "publish" else {}
        return action, {"number": args.number, **extra}
    if action == "send":
        return action, {"agent": args.agent, "message": " ".join(args.message)}
    if action == "stop":
        return action, {"agent": args.agent}
    if action == "wait":
        return action, {"reason": " ".join(args.reason)}
    return action, {}  # approve, prs


def run(args: argparse.Namespace, ws: Workspace | None = None, env: dict | None = None,
        wait_seconds: float = ANSWER_SECONDS) -> int:
    env = os.environ if env is None else env
    slug, sender = env.get("JEF_PROJECT", ""), env.get("JEF_AGENT", "")
    ws = ws or Workspace.default()
    project = load_project(ws, slug) if slug else None
    if not slug or not sender or project is None or project.coordinator_agent != sender:
        print(NOT_A_COORDINATOR)
        return 2

    if args.action == "status":
        print(render_status(ws, slug, load_roster(ws, slug)))
        return 0
    if args.action == "read":
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
        print("Jef is still working on that. Check with `jef project status` in a little while.")
        return 1
    print(answer.get("message", ""))
    return 0 if answer.get("ok") else 1
