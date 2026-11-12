"""Jef's local web UI: the backlog, how every project is going, and settings."""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from jef import claude, jefchat
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Chats, ChatError, Status
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus, github_status
from jef.pipeline import IMPLEMENTERS_AT_ONCE, Phase, Pipeline, PipelineError
from jef.projects import StartError, gh_clone, known_repos, load_project, parse_repo, projects_on_repo, start_project
from jef.tasks import TaskList, TaskStatus
from jef.usage import Usage, agent_usage, format_cost, format_tokens, project_total, project_usage
from jef.workspace import Workspace

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["tokens"] = format_tokens
TEMPLATES.env.filters["cost"] = format_cost


@dataclass
class ProjectSummary:
    slug: str
    name: str
    total: int
    counts: dict[TaskStatus, int]
    usage: Usage = Usage()

    @property
    def merged(self) -> int:
        return self.counts[TaskStatus.MERGED]


def summarize_projects(ws: Workspace) -> list[ProjectSummary]:
    summaries = []
    for slug in ws.project_slugs():
        tasks = TaskList.load(ws.project_dir(slug) / "tasks.md")
        summaries.append(
            ProjectSummary(
                slug=slug,
                name=tasks.title or slug,
                total=len(tasks.tasks),
                counts=tasks.counts(),
                usage=project_total(ws, slug),
            )
        )
    return summaries


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
) -> FastAPI:
    """``claude_status`` is the result of the check ``jef`` ran on startup, if any."""
    ws = workspace or Workspace.default()
    app = FastAPI(title="Jef", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.claude = claude_status
    app.state.chats = chats
    app.state.jef_chats = jef_chats
    app.state.pipeline = pipeline

    def get_chats() -> Chats | None:
        """Agents can only run once Claude Code has been checked and works."""
        status = app.state.claude
        if app.state.chats is None and status is not None and status.ok:
            app.state.chats = Chats(ws, status.path)
        return app.state.chats

    def get_jef_chats() -> Chats | None:
        """The home page's own chat, kept apart from the projects."""
        status = app.state.claude
        if app.state.jef_chats is None and status is not None and status.ok:
            app.state.jef_chats = jefchat.make_chats(ws, status.path)
        return app.state.jef_chats

    def get_pipeline() -> Pipeline | None:
        runner = get_chats()
        if app.state.pipeline is None and runner is not None:
            app.state.pipeline = Pipeline(ws, runner)
        return app.state.pipeline

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
    def home(request: Request, error: str = ""):
        backlog = Backlog.load(ws.backlog_file)
        jef_runner = get_jef_chats()
        jef_agent = jefchat.agent(jef_runner) if jef_runner else None
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
                "projects": summarize_projects(ws),
                "statuses": list(TaskStatus),
                "workspace": ws.root,
                "claude": app.state.claude,
                "coordinator_pages": coordinator_pages(),
                "waiting": BacklogStatus.WAITING,
                "error": error,
            },
        )

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

    @app.post("/backlog")
    def add_backlog_item(feature: str = Form("")):
        backlog = Backlog.load(ws.backlog_file)
        try:
            backlog.add(feature)
        except ValueError:
            return RedirectResponse("/?error=empty", status_code=303)
        backlog.save(ws.backlog_file)
        return RedirectResponse("/", status_code=303)

    def find_item(number: int):
        return next((i for i in Backlog.load(ws.backlog_file).items if i.number == number), None)

    @app.post("/backlog/{number}/delete")
    def delete_backlog_item(number: int):
        backlog = Backlog.load(ws.backlog_file)
        try:
            backlog.remove(number)
        except ValueError:
            return RedirectResponse("/?error=delete", status_code=303)
        backlog.save(ws.backlog_file)
        return RedirectResponse("/", status_code=303)

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
                "heartbeat": flow.heartbeat_minutes(slug) if flow else 60,
                "last_checked": ago(flow.now() - checked) if checked is not None else "",
                "at_once": IMPLEMENTERS_AT_ONCE,
                "usage": usage,
                "usage_total": sum(usage.values(), Usage()),
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
            return RedirectResponse(f"/projects/{slug}?error=" + ("busy" if "still working" in str(err) else "stopped"), status_code=303)
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
                "claude": app.state.claude,
                "github": github_check(),
                "notice": notice,
                "error": error,
            },
        )

    @app.post("/settings/claude-check")
    def check_claude_again():
        app.state.claude = claude_check()
        return RedirectResponse("/settings?notice=checked", status_code=303)

    return app
