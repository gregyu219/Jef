from fastapi.testclient import TestClient

from jef.backlog import Backlog
from jef.web.app import create_app
from jef.workspace import Workspace


def make_client(tmp_path):
    ws = Workspace(tmp_path)
    return ws, TestClient(create_app(ws))


def test_home_page_shows_empty_states(tmp_path):
    _, client = make_client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert "Your backlog is empty" in page.text
    assert "No projects yet" in page.text


def test_adding_a_backlog_item_saves_it_and_shows_it(tmp_path):
    ws, client = make_client(tmp_path)
    response = client.post("/backlog", data={"feature": "Weekly email summary"})
    assert response.status_code == 200  # followed the redirect back home
    assert "Weekly email summary" in response.text
    assert Backlog.load(ws.backlog_file).items[0].feature == "Weekly email summary"


def test_blank_backlog_item_shows_an_error(tmp_path):
    ws, client = make_client(tmp_path)
    response = client.post("/backlog", data={"feature": "  "})
    assert "Type a description first" in response.text
    assert not ws.backlog_file.exists()


def test_projects_show_merged_counts(tmp_path):
    ws, client = make_client(tmp_path)
    project = ws.project_dir("todo-app")
    project.mkdir(parents=True)
    (project / "tasks.md").write_text(
        "# Tasks: To-do list app\n\n| # | Task | Status | Agent | PR |\n| --- | --- | --- | --- | --- |\n"
        "| 1 | Set up | Merged | impl-1 | #1 |\n| 2 | List | Implementation | impl-2 | |\n"
    )
    page = client.get("/")
    assert "To-do list app" in page.text
    assert "1 of 2" in page.text


def test_user_text_is_escaped(tmp_path):
    _, client = make_client(tmp_path)
    response = client.post("/backlog", data={"feature": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
