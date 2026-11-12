import pytest
from fastapi.testclient import TestClient

from jef.backlog import Backlog
from jef.github import GitHubStatus
from jef.secrets import ANTHROPIC_API_KEY
from jef.web.app import create_app
from jef.workspace import Workspace

FAKE_KEY = "sk-ant-api03-" + "x" * 40 + "WXYZ"


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


@pytest.fixture
def client(ws, secret_store):
    app = create_app(ws, secrets=secret_store, github_check=lambda: GitHubStatus(True, "octocat"))
    return TestClient(app)


def test_home_page_shows_empty_states_and_asks_for_a_key(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Your backlog is empty" in page.text
    assert "No projects yet" in page.text
    assert "needs your Anthropic API key" in page.text


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


def test_saving_a_key_stores_it_in_the_keychain_not_the_workspace(ws, client, secret_store):
    page = client.post("/settings/anthropic-key", data={"key": f"  {FAKE_KEY}  "})
    assert "Key saved" in page.text
    assert secret_store.get(ANTHROPIC_API_KEY) == FAKE_KEY
    # The full key is never shown back, and nothing was written to disk.
    assert FAKE_KEY not in page.text
    assert "sk-ant-…WXYZ" in page.text
    assert not any(FAKE_KEY in p.read_text() for p in ws.root.rglob("*") if p.is_file())
    assert "needs your Anthropic API key" not in client.get("/").text


def test_key_that_is_not_an_anthropic_key_is_rejected(client, secret_store):
    page = client.post("/settings/anthropic-key", data={"key": "hello"})
    assert "doesn't look like an Anthropic API key" in page.text
    assert secret_store.get(ANTHROPIC_API_KEY) is None


def test_removing_the_key(client, secret_store):
    secret_store.set(ANTHROPIC_API_KEY, FAKE_KEY)
    page = client.post("/settings/anthropic-key/delete")
    assert "Key removed" in page.text
    assert secret_store.get(ANTHROPIC_API_KEY) is None


def test_settings_shows_github_account(client):
    assert "octocat" in client.get("/settings").text


def test_settings_explains_missing_github_login(ws, secret_store):
    app = create_app(ws, secrets=secret_store, github_check=lambda: GitHubStatus(True, ""))
    assert "gh auth login" in TestClient(app).get("/settings").text
