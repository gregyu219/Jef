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
from jef.secrets import ANTHROPIC_API_KEY, SecretStore, mask
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
    secrets: SecretStore | None = None,
    github_check: Callable[[], GitHubStatus] = github_status,
) -> FastAPI:
    ws = workspace or Workspace.default()
    store = secrets or SecretStore()
    app = FastAPI(title="Jef", docs_url=None, redoc_url=None, openapi_url=None)

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
                "key_saved": store.get(ANTHROPIC_API_KEY) is not None,
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
                "key_masked": mask(store.get(ANTHROPIC_API_KEY)),
                "github": github_check(),
                "notice": notice,
                "error": error,
            },
        )

    @app.post("/settings/anthropic-key")
    def save_key(key: str = Form("")):
        key = key.strip()
        if not key:
            return RedirectResponse("/settings?error=empty", status_code=303)
        if not key.startswith("sk-ant-"):
            return RedirectResponse("/settings?error=shape", status_code=303)
        store.set(ANTHROPIC_API_KEY, key)
        return RedirectResponse("/settings?notice=saved", status_code=303)

    @app.post("/settings/anthropic-key/delete")
    def delete_key():
        store.delete(ANTHROPIC_API_KEY)
        return RedirectResponse("/settings?notice=removed", status_code=303)

    return app
