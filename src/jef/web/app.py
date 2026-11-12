"""Jef's local web UI: the backlog, how every project is going, and settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from jef.backlog import Backlog
from jef.github import GitHubStatus, github_status
from jef import claude
from jef.claude import ClaudeCheck
from jef.tasks import TaskList, TaskStatus
from jef.workspace import Workspace

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class ProjectSummary:
    slug: str
    name: str
    total: int
    counts: dict[TaskStatus, int]

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
            )
        )
    return summaries


def create_app(
    workspace: Workspace | None = None,
    github_check: Callable[[], GitHubStatus] = github_status,
    claude_status: ClaudeCheck | None = None,
    claude_check: Callable[[], ClaudeCheck] = claude.check,
) -> FastAPI:
    """``claude_status`` is the result of the check ``jef`` ran on startup, if any."""
    ws = workspace or Workspace.default()
    app = FastAPI(title="Jef", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.claude = claude_status

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, error: str = ""):
        backlog = Backlog.load(ws.backlog_file)
        return TEMPLATES.TemplateResponse(
            request,
            "home.html",
            {
                "page": "home",
                "backlog": backlog.items,
                "projects": summarize_projects(ws),
                "statuses": list(TaskStatus),
                "workspace": ws.root,
                "claude": app.state.claude,
                "error": error,
            },
        )

    @app.post("/backlog")
    def add_backlog_item(feature: str = Form("")):
        backlog = Backlog.load(ws.backlog_file)
        try:
            backlog.add(feature)
        except ValueError:
            return RedirectResponse("/?error=empty", status_code=303)
        backlog.save(ws.backlog_file)
        return RedirectResponse("/", status_code=303)

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
