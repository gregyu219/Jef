"""Jef's local web UI: the backlog and how every project is going."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from jef.backlog import Backlog
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


def create_app(workspace: Workspace | None = None) -> FastAPI:
    ws = workspace or Workspace.default()
    app = FastAPI(title="Jef")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, error: str = ""):
        backlog = Backlog.load(ws.backlog_file)
        return TEMPLATES.TemplateResponse(
            request,
            "home.html",
            {
                "backlog": backlog.items,
                "projects": summarize_projects(ws),
                "statuses": list(TaskStatus),
                "workspace": ws.root,
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

    return app
