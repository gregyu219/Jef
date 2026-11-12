import pytest
from fastapi.testclient import TestClient

from jef.backlog import Backlog
from jef.github import GitHubStatus
from jef.claude import ClaudeCheck
from jef.web.app import create_app
from jef.workspace import Workspace

@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


@pytest.fixture
def client(ws):
    app = create_app(ws, github_check=lambda: GitHubStatus(True, "octocat"))
    return TestClient(app)


def test_home_page_shows_empty_states(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Your backlog is empty" in page.text
    assert "No projects yet" in page.text


def test_adding_a_backlog_item_saves_it_and_shows_it(ws, client):
    response = client.post("/backlog", data={"feature": "Weekly email summary"})
    assert response.status_code == 200  # followed the redirect back home
    assert "Weekly email summary" in response.text
    assert Backlog.load(ws.backlog_file).items[0].feature == "Weekly email summary"


def test_blank_backlog_item_shows_an_error(ws, client):
    response = client.post("/backlog", data={"feature": "  "})
    assert "Type a description first" in response.text
    assert not ws.backlog_file.exists()


def test_projects_show_merged_counts(ws, client):
    project = ws.project_dir("todo-app")
    project.mkdir(parents=True)
    (project / "tasks.md").write_text(
        "# Tasks: To-do list app\n\n| # | Task | Status | Agent | PR |\n| --- | --- | --- | --- | --- |\n"
        "| 1 | Set up | Merged | impl-1 | #1 |\n| 2 | List | Implementation | impl-2 | |\n"
    )
    page = client.get("/")
    assert "To-do list app" in page.text
    assert "1 of 2" in page.text


def test_user_text_is_escaped(client):
    response = client.post("/backlog", data={"feature": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_settings_shows_claude_code_is_working(ws):
    app = create_app(ws, github_check=lambda: GitHubStatus(True, "octocat"), claude_status=ClaudeCheck(True, path="/opt/claude"))
    page = TestClient(app).get("/settings").text
    assert "Working." in page and "/opt/claude" in page
    assert "can't run until Claude Code works" not in TestClient(app).get("/").text


def test_broken_claude_code_shows_what_to_do(ws):
    app = create_app(ws, github_check=lambda: GitHubStatus(True, "o"), claude_status=ClaudeCheck(False, "failed", "Not logged in"))
    c = TestClient(app)
    assert "can't run until Claude Code works" in c.get("/").text
    settings = c.get("/settings").text
    assert "Not logged in" in settings and "/login" in settings


def test_check_again_reruns_the_check(ws):
    calls = []
    app = create_app(
        ws,
        github_check=lambda: GitHubStatus(True, "o"),
        claude_status=ClaudeCheck(False, "missing"),
        claude_check=lambda: calls.append(1) or ClaudeCheck(True, path="/bin/claude"),
    )
    page = TestClient(app).post("/settings/claude-check").text
    assert calls == [1]
    assert "Checked just now" in page and "Working." in page


def test_settings_shows_github_account(client):
    assert "octocat" in client.get("/settings").text


def test_settings_explains_missing_github_login(ws):
    app = create_app(ws, github_check=lambda: GitHubStatus(True, ""))
    assert "gh auth login" in TestClient(app).get("/settings").text
