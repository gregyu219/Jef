"""Jef's local web UI: the backlog, how every project is going, and settings."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from jef import claude, jefchat, models
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Agent, Chats, ChatError, Role, Status, load_roster
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus, github_status
from jef.pipeline import Phase, Pipeline, PipelineError, pr_label
from jef.runtime import Runtime
from jef.projects import (
    MAX_IMPLEMENTERS_AT_ONCE,
    DeleteError,
    StartError,
    auto_approve,
    delete_project,
    gh_clone,
    goal_fields,
    implementers_at_once,
    known_repos,
    load_project,
    parse_repo,
    project_for_backlog_item,
    projects_on_repo,
    set_goal_field,
    start_project,
    used_backlog_numbers,
)
from jef.status import NEEDS_REVIEW, Attention, needs_you, project_phase, read_status, status_path, write_status
from jef.tasks import TaskList, TaskStatus
from jef.usage import (
    Usage,
    agent_usage,
    budget_usd,
    format_cost,
    format_rate,
    format_tokens,
    latest_plan_usage,
    project_total,
    project_usage,
)
from jef.workspace import Workspace


def format_reset(when: datetime, now: datetime) -> str:
    """A reset time in local time: "3:00 pm" today, else "Fri 14 Nov, 9:00 am"."""
    local, today = when.astimezone(), now.astimezone()
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"
    if local.date() == today.date():
        return clock
    return f"{local:%a} {local.day} {local:%b}, {clock}"


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["tokens"] = format_tokens
TEMPLATES.env.filters["cost"] = format_cost
TEMPLATES.env.filters["rate"] = format_rate
TEMPLATES.env.filters["reset"] = format_reset
TEMPLATES.env.filters["pr_label"] = pr_label
# Said by every Delete button that takes a project with it.
TEMPLATES.env.globals["project_delete_note"] = (
    "This stops its agents, deletes its local folder (clone, chats, tasks and its cost history) "
    "and removes it from the backlog. Its GitHub branches and PRs stay."
)

# The project page's model form: each level's field name and label.
MODEL_FORM: dict[Role, tuple[str, str]] = {
    Role.COORDINATOR: ("coordinator", "Coordinator"),
    Role.RESEARCH: ("research", "Research"),
    Role.IMPLEMENTER: ("implementer", "Implementers"),
}


@dataclass
class ProjectSummary:
    slug: str
    name: str
    total: int
    counts: dict[TaskStatus, int]
    usage: Usage = Usage()
    # From status.md, so the home page needs no LLM call to show them.
    phase: str = ""
    coordinator: str = ""
    coordinator_status: str = ""
    problem: str = ""

    @property
    def merged(self) -> int:
        return self.counts[TaskStatus.MERGED]


def split_coordinator(value: str) -> tuple[str, str]:
    """"Katherine (Idle)" -> ("Katherine", "Idle"); "none" -> ("", "")."""
    value = value.strip()
    if not value or value == "none":
        return "", ""
    name, paren, rest = value.rpartition(" (")
    if paren and rest.endswith(")"):
        return name, rest[:-1]
    return value, ""


def summarize_projects(ws: Workspace) -> list[ProjectSummary]:
    summaries = []
    for slug in ws.project_slugs():
        tasks = TaskList.load(ws.project_dir(slug) / "tasks.md")
        status = read_status(ws, slug)
        coordinator, coordinator_status = split_coordinator(status.get("coordinator", ""))
        problem = status.get("problem", "")
        summaries.append(
            ProjectSummary(
                slug=slug,
                name=tasks.title or slug,
                total=len(tasks.tasks),
                counts=tasks.counts(),
                usage=project_total(ws, slug),
                phase=status.get("phase", ""),
                coordinator=coordinator,
                coordinator_status=coordinator_status,
                problem="" if problem == "none" else problem,
            )
        )
    return summaries


def refresh_statuses(ws: Workspace, roster: Callable[[str], list[Agent]] | None = None) -> int:
    """Rewrite every project's status.md from its files. ``roster`` gives a
    project's agents (Chats.refresh when Claude Code works); without it the
    saved agents.md is read. No LLM or GitHub call. Returns how many changed."""
    changed = 0
    for slug in ws.project_slugs():
        agents = roster(slug) if roster else load_roster(ws, slug)
        changed += write_status(ws, slug, agents)
    return changed


def backlog_projects(ws: Workspace) -> dict[int, str]:
    """Backlog item number -> the slug of the project started from it."""
    found = {}
    for slug in ws.project_slugs():
        number = goal_fields(ws, slug).get("backlog item", "").lstrip("#").strip()
        if number.isdigit():
            found[int(number)] = slug
    return found


def backlog_attention(ws: Workspace, backlog: Backlog) -> dict[int, tuple[str, Attention]]:
    """For each in-progress backlog item waiting on you: (its project's slug,
    what it needs). Read from the project files on every page load, so it's
    never stale and costs no LLM or GitHub call."""
    slugs = backlog_projects(ws)
    found = {}
    for item in backlog.items:
        slug = slugs.get(item.number)
        if item.status is not BacklogStatus.IN_PROGRESS or slug is None:
            continue
        attention = needs_you(ws, slug, load_roster(ws, slug))
        if attention is not None:
            found[item.number] = (slug, attention)
    return found


def backlog_auto_approve(ws: Workspace, backlog: Backlog) -> dict[int, tuple[bool, bool]]:
    """For each backlog item: (whether Auto approve is on, whether it can
    still be changed). A started item reads its project's goal.md, the
    source of truth; a Waiting one reads its backlog row. It stops being
    changeable once the item is done or its task list is already approved."""
    slugs = backlog_projects(ws)
    found = {}
    for item in backlog.items:
        slug = slugs.get(item.number)
        if slug is None:
            found[item.number] = (item.auto_approve, item.status is not BacklogStatus.DONE)
            continue
        phase = project_phase(ws, slug, load_roster(ws, slug))
        changeable = item.status is not BacklogStatus.DONE and phase not in (Phase.BUILDING, Phase.DONE, Phase.STOPPED)
        found[item.number] = (auto_approve(ws, slug), changeable)
    return found


def ago(seconds: float) -> str:
    """How long ago, in words: "just now", "5 minutes ago", "2 hours ago"."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'} ago"


def create_app(
    workspace: Workspace | None = None,
    github_check: Callable[[], GitHubStatus] = github_status,
    claude_status: ClaudeCheck | None = None,
    claude_check: Callable[[], ClaudeCheck] = claude.check,
    chats: Chats | None = None,
    clone: Callable[[str, Path], None] = gh_clone,
    pipeline: Pipeline | None = None,
    choose_name: Callable[[list[str]], str] = random.choice,
    jef_chats: Chats | None = None,
    on_agents_ready: Callable[[Pipeline], None] | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    """``claude_status`` is the result of the check ``jef`` ran on startup, if any.

    Pass a ``runtime`` to share its chats and pipeline with Jef's background
    threads; otherwise one is made from ``claude_status``, ``chats``,
    ``jef_chats`` and ``pipeline``."""
    if runtime is None:
        runtime = Runtime(
            workspace or Workspace.default(),
            claude_status=claude_status,
            chats=chats,
            jef_chats=jef_chats,
            pipeline=pipeline,
        )
    ws = runtime.workspace
    if on_agents_ready is not None:
        runtime.on_agents_ready(on_agents_ready)
    app = FastAPI(title="Jef", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    get_chats = runtime.get_chats
    get_jef_chats = runtime.get_jef_chats
    get_pipeline = runtime.get_pipeline

    def known(slug: str):
        return load_project(ws, slug) if slug in ws.project_slugs() else None

    def coordinator_pages() -> dict[str, str]:
        """Coordinator name -> its project's slug, for linking from the backlog."""
        pages = {}
        for slug in ws.project_slugs():
            project = load_project(ws, slug)
            if project and project.coordinator:
                pages[project.coordinator] = slug
        return pages

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, error: str = "", status: str = ""):
        backlog = Backlog.load(ws.backlog_file)
        jef_runner = get_jef_chats()
        jef_agent = jefchat.agent(jef_runner) if jef_runner else None
        projects = summarize_projects(ws)
        now = datetime.now(timezone.utc)
        plan = latest_plan_usage(ws)
        return TEMPLATES.TemplateResponse(
            request,
            "home.html",
            {
                "page": "home",
                "jef_ok": jef_runner is not None,
                "jef_chat": jef_runner.transcript(jefchat.HOME, jefchat.NAME) if jef_runner else [],
                "jef_agent": jef_agent,
                "jef_working": jef_agent is not None and jef_agent.status is Status.WORKING,
                "backlog": backlog.items,
                "projects": projects,
                "total_cost": sum(p.usage.cost_usd for p in projects),
                "plan_usage": plan,
                "plan_seen": ago((now - plan.seen_at).total_seconds()) if plan and plan.seen_at else "",
                "now": now,
                "statuses": list(TaskStatus),
                "workspace": ws.root,
                "claude": runtime.claude,
                "coordinator_pages": coordinator_pages(),
                "attention": backlog_attention(ws, backlog),
                "item_projects": backlog_projects(ws),
                "auto_approve": backlog_auto_approve(ws, backlog),
                "needs_review": NEEDS_REVIEW,
                "waiting": BacklogStatus.WAITING,
                "error": error,
                "status_read": status == "read",
            },
        )

    @app.post("/status/refresh")
    def refresh_status():
        """The Read status button: rewrite every status.md from Jef's own
        files. No LLM or GitHub call, and nobody is nudged."""
        flow = get_pipeline()
        if flow is not None:
            for slug in ws.project_slugs():
                flow.write_status(slug)
        else:
            refresh_statuses(ws)
        return RedirectResponse("/?status=read#projects", status_code=303)

    @app.get("/projects/{slug}/status", response_class=PlainTextResponse)
    def project_status(slug: str):
        path = status_path(ws, slug)
        if known(slug) is None or not path.exists():
            return PlainTextResponse("No status yet.\n", status_code=404)
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.post("/jef/message")
    def message_jef(message: str = Form("")):
        if not message.strip():
            return RedirectResponse("/?error=jef-empty#jef", status_code=303)
        runner = get_jef_chats()
        if runner is None:
            return RedirectResponse("/?error=jef-claude#jef", status_code=303)
        try:
            jefchat.send(runner, ws, message.strip())
        except ChatError as err:
            return RedirectResponse("/?error=" + ("jef-busy" if "still working" in str(err) else "jef-failed") + "#jef", status_code=303)
        return RedirectResponse("/#jef", status_code=303)

    @app.post("/jef/reset")
    def reset_jef():
        runner = get_jef_chats()
        if runner is None:
            return RedirectResponse("/?error=jef-claude#jef", status_code=303)
        try:
            jefchat.reset(runner, ws)
        except ChatError:
            return RedirectResponse("/?error=jef-failed#jef", status_code=303)
        return RedirectResponse("/#jef", status_code=303)

    @app.post("/backlog")
    def add_backlog_item(feature: str = Form("")):
        backlog = Backlog.load(ws.backlog_file)
        try:
            backlog.add(feature, reserved=used_backlog_numbers(ws))
        except ValueError:
            return RedirectResponse("/?error=empty", status_code=303)
        backlog.save(ws.backlog_file)
        return RedirectResponse("/", status_code=303)

    def find_item(number: int):
        return next((i for i in Backlog.load(ws.backlog_file).items if i.number == number), None)

    def remove_project(slug: str) -> None:
        """Stop the project's agents, delete its folder and its backlog row."""
        delete_project(ws, slug, get_chats())
        if runtime.pipeline is not None:
            runtime.pipeline.forget(slug)

    @app.post("/backlog/{number}/delete")
    def delete_backlog_item(number: int):
        """Delete an item in any state. If it has a project, that goes too."""
        slug = project_for_backlog_item(ws, number)
        try:
            if slug is not None:
                remove_project(slug)
            else:
                backlog = Backlog.load(ws.backlog_file)
                backlog.remove(number)
                backlog.save(ws.backlog_file)
        except (ValueError, DeleteError):
            return RedirectResponse("/?error=delete", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/backlog/{number}/auto-approve")
    def set_backlog_auto_approve(number: int, on: str = Form("")):
        """The backlog's Auto approve toggle. A started item's goal.md is
        what the heartbeat reads, so it changes too; the backlog row always
        does, so the two stay the same."""
        value = on == "yes"
        backlog = Backlog.load(ws.backlog_file)
        item = next((i for i in backlog.items if i.number == number), None)
        if item is None:
            return RedirectResponse("/#backlog", status_code=303)
        slug = project_for_backlog_item(ws, number)
        if slug is not None:
            flow = get_pipeline()
            if flow is not None:
                flow.set_auto_approve(slug, value)
                flow.write_status(slug)
            else:
                set_goal_field(ws, slug, "Auto approve", "yes" if value else "no")
                write_status(ws, slug, load_roster(ws, slug))
        item.auto_approve = value
        backlog.save(ws.backlog_file)
        return RedirectResponse("/#backlog", status_code=303)

    @app.post("/projects/{slug}/delete")
    def delete_project_page(slug: str):
        # Any project folder, even a broken one with no goal.md, can go.
        if slug not in ws.project_slugs():
            return RedirectResponse("/", status_code=303)
        try:
            remove_project(slug)
        except DeleteError:
            return RedirectResponse("/?error=project-delete#projects", status_code=303)
        return RedirectResponse("/#projects", status_code=303)

    @app.get("/backlog/{number}/start", response_class=HTMLResponse)
    def start_form(request: Request, number: int, error: str = "", repo: str = ""):
        item = find_item(number)
        if item is None:
            return RedirectResponse("/", status_code=303)
        repos = known_repos(ws)
        chosen = repo or (repos[0] if repos else "")
        try:
            others = projects_on_repo(ws, parse_repo(chosen)) if chosen else []
        except StartError:
            others = []
        return TEMPLATES.TemplateResponse(
            request,
            "start.html",
            {
                "page": "home",
                "item": item,
                "started": item.status is not BacklogStatus.WAITING,
                "github": github_check(),
                "claude_ok": get_chats() is not None,
                "repo": chosen,
                "known_repos": repos,
                "others": others,
                "error": error,
            },
        )

    @app.post("/backlog/{number}/start", response_class=HTMLResponse)
    def start(request: Request, number: int, repo: str = Form(""), confirm: str = Form("")):
        item = find_item(number)
        if item is None:
            return RedirectResponse("/", status_code=303)

        def again(error: str):
            return start_form(request, number, error=error, repo=repo)

        if confirm != "yes":
            return again("Tick the box to confirm you want to start.")
        runner = get_chats()
        if runner is None:
            return again("Claude Code isn't working yet, so no agent can start. See Settings.")
        try:
            project = start_project(ws, number, repo, runner, clone=clone, choose_name=choose_name)
        except StartError as err:
            return again(str(err))
        return RedirectResponse(f"/projects/{project.slug}", status_code=303)

    @app.get("/projects/{slug}", response_class=HTMLResponse)
    def project_page(request: Request, slug: str, error: str = ""):
        project = known(slug)
        if project is None:
            return RedirectResponse("/", status_code=303)
        runner = get_chats()
        flow = get_pipeline()
        agents = runner.refresh(slug) if runner else []
        coordinator = next((a for a in agents if a.name == project.coordinator_agent), None)
        usage = project_usage(ws, slug)
        checked = flow.last_checked(slug) if flow else None
        return TEMPLATES.TemplateResponse(
            request,
            "project.html",
            {
                "page": "home",
                "project": project,
                "agents": agents,
                "coordinator": coordinator,
                "chat": runner.transcript(slug, project.coordinator_agent) if runner else [],
                "tasks": TaskList.load(ws.project_dir(slug) / "tasks.md").tasks,
                "working": any(a.status is Status.WORKING for a in agents),
                "claude_ok": runner is not None,
                "phase": flow.phase(slug) if flow else None,
                "phases": Phase,
                "problem": flow.problem(slug) if flow else "",
                "research": flow.research(slug) if flow else "",
                "status_text": status_path(ws, slug).read_text(encoding="utf-8") if status_path(ws, slug).exists() else "",
                "heartbeat": flow.heartbeat_minutes(slug) if flow else 60,
                "coordinator_decides": flow.coordinator_decides(slug) if flow else True,
                "models": [
                    (name, label, models.model_setting(ws, slug, role)) for role, (name, label) in MODEL_FORM.items()
                ],
                "model_choices": models.CHOICES,
                "last_checked": ago(flow.now() - checked) if checked is not None else "",
                "at_once": implementers_at_once(ws, slug),
                "max_at_once": MAX_IMPLEMENTERS_AT_ONCE,
                "usage": usage,
                "usage_total": sum(usage.values(), Usage()),
                "budget": budget_usd(ws, slug),
                "no_usage": Usage(),
                "error": error,
            },
        )

    def pipeline_action(slug: str, action: Callable[[Pipeline], None]):
        flow = get_pipeline()
        if known(slug) is None:
            return RedirectResponse("/", status_code=303)
        if flow is None:
            return RedirectResponse(f"/projects/{slug}?error=claude", status_code=303)
        try:
            action(flow)
        except (PipelineError, ChatError) as err:
            from urllib.parse import quote

            return RedirectResponse(f"/projects/{slug}?error=" + quote(str(err)), status_code=303)
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.post("/projects/{slug}/research")
    def start_research(slug: str):
        return pipeline_action(slug, lambda flow: flow.start_research(slug))

    @app.post("/projects/{slug}/approve")
    def approve(slug: str):
        return pipeline_action(slug, lambda flow: flow.approve_tasks(slug))

    @app.post("/projects/{slug}/retry")
    def retry(slug: str):
        return pipeline_action(slug, lambda flow: flow.clear_problem(slug))

    @app.post("/projects/{slug}/check")
    def check_now(slug: str):
        return pipeline_action(slug, lambda flow: flow.check_now(slug))

    @app.post("/projects/{slug}/heartbeat")
    def set_heartbeat(slug: str, minutes: str = Form("")):
        if not minutes.strip().isdigit():
            return RedirectResponse(f"/projects/{slug}?error=Type+a+number+of+minutes.", status_code=303)
        return pipeline_action(slug, lambda flow: flow.set_heartbeat_minutes(slug, int(minutes)))

    @app.post("/projects/{slug}/implementers")
    def set_implementers(slug: str, count: str = Form("")):
        if not count.strip().isdigit():
            return RedirectResponse(f"/projects/{slug}?error=Type+a+number+of+tasks.", status_code=303)
        return pipeline_action(slug, lambda flow: flow.set_implementers_at_once(slug, int(count)))

    @app.post("/projects/{slug}/models")
    def set_models(slug: str, coordinator: str = Form(""), research: str = Form(""), implementer: str = Form("")):
        given = {"coordinator": coordinator, "research": research, "implementer": implementer}
        chosen = {role: given[name] for role, (name, _) in MODEL_FORM.items() if given[name].strip()}
        return pipeline_action(slug, lambda flow: flow.set_models(slug, chosen))

    @app.post("/projects/{slug}/coordinator-decides")
    def set_coordinator_decides(slug: str, on: str = Form("")):
        return pipeline_action(slug, lambda flow: flow.set_coordinator_decides(slug, on == "yes"))

    @app.post("/projects/{slug}/wake")
    def wake(slug: str):
        return pipeline_action(slug, lambda flow: flow.wake_now(slug))

    @app.post("/projects/{slug}/budget")
    def set_budget(slug: str, amount: str = Form("")):
        text = amount.strip().removeprefix("$").replace(",", "")
        if text.lower() in ("", "none"):
            value = None
        else:
            try:
                value = float(text)
            except ValueError:
                return RedirectResponse(f"/projects/{slug}?error=Type+a+dollar+amount,+like+20.", status_code=303)
        return pipeline_action(slug, lambda flow: flow.set_budget_usd(slug, value))

    @app.get("/projects/{slug}/agents/{name}", response_class=HTMLResponse)
    def agent_page(request: Request, slug: str, name: str):
        project = known(slug)
        runner = get_chats()
        agent = next((a for a in runner.refresh(slug) if a.name == name), None) if (project and runner) else None
        if agent is None:
            return RedirectResponse(f"/projects/{slug}" if project else "/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "agent.html",
            {
                "page": "home",
                "project": project,
                "agent": agent,
                "chat": runner.transcript(slug, name),
                "usage": agent_usage(runner.chat_file(slug, name)),
                "working": agent.status is Status.WORKING,
            },
        )

    @app.post("/projects/{slug}/message")
    def message_coordinator(slug: str, message: str = Form("")):
        project = known(slug)
        runner = get_chats()
        if project is None:
            return RedirectResponse("/", status_code=303)
        if not message.strip():
            return RedirectResponse(f"/projects/{slug}?error=empty", status_code=303)
        if runner is None:
            return RedirectResponse(f"/projects/{slug}?error=claude", status_code=303)
        try:
            runner.send(slug, project.coordinator_agent, message.strip())
        except ChatError as err:
            said = str(err)
            reason = "busy" if "still working" in said else "budget" if "budget" in said else "stopped"
            return RedirectResponse(f"/projects/{slug}?error={reason}", status_code=303)
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.post("/projects/{slug}/stop")
    def stop_project(slug: str):
        project = known(slug)
        runner = get_chats()
        if project is not None and runner is not None:
            try:
                runner.stop(slug, project.coordinator_agent)
            except ChatError:
                pass
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, notice: str = "", error: str = ""):
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "page": "settings",
                "claude": runtime.claude,
                "github": github_check(),
                "notice": notice,
                "error": error,
            },
        )

    @app.post("/settings/claude-check")
    def check_claude_again():
        runtime.claude = claude_check()
        get_pipeline()  # if it works now, agents (and Jef's timer) can start
        return RedirectResponse("/settings?notice=checked", status_code=303)

    return app
