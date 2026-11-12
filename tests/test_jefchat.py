import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from jef import jefchat
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Chats, ChatError, Role, load_roster
from jef.github import GitHubStatus
from jef.web.app import create_app
from jef.workspace import Workspace


class FakeProc:
    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs, self.done = cmd, kwargs, False
        self.stdin_text = kwargs["stdin"].read().decode()

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.done = True


@pytest.fixture
def env(tmp_path):
    ws = Workspace(tmp_path / "jef-home")
    backlog = Backlog()
    backlog.add("Weekly email summary")
    backlog.add("Dark mode")
    backlog.items[0].status = BacklogStatus.IN_PROGRESS
    backlog.items[0].coordinator = "Ada"
    backlog.save(ws.backlog_file)
    project = ws.project_dir("weekly-email-summary")
    project.mkdir(parents=True)
    (project / "goal.md").write_text(
        "# Goal: Weekly email summary\n\n- Repo: octo/mail\n- Coordinator: Ada\n- Backlog item: #1\n"
    )
    (project / "tasks.md").write_text(
        "# Tasks: Weekly email summary\n\n| # | Task | Status | Agent | PR |\n| --- | --- | --- | --- | --- |\n"
        "| 1 | Set up | Merged | impl-1 | #1 |\n| 2 | Send it | Implementation | impl-2 | |\n"
    )
    procs = []

    def popen(cmd, **kwargs):
        procs.append(FakeProc(cmd, **kwargs))
        return procs[-1]

    ids = iter(["session-1", "session-2", "session-3"])
    chats = Chats(jefchat.jef_workspace(ws), "/bin/claude", popen=popen, new_session_id=lambda: next(ids))
    return ws, chats, procs


def test_context_message_briefs_on_backlog_and_projects(env):
    ws, _, _ = env
    text = jefchat.context_message(ws)
    assert "#1 Weekly email summary (In progress, coordinator Ada)" in text
    assert "#2 Dark mode (Waiting)" in text
    assert "repo octo/mail" in text and "1 of 2 tasks merged" in text
    assert str(ws.root) in text


def test_context_message_with_nothing_yet(tmp_path):
    text = jefchat.context_message(Workspace(tmp_path))
    assert "(empty)" in text and "(none yet)" in text


def test_first_message_starts_a_session_with_the_briefing(env):
    ws, chats, procs = env
    agent = jefchat.send(chats, ws, "How's it going?")
    assert agent.role is Role.JEF and agent.session == "session-1"
    cmd = procs[0].cmd
    assert "--session-id" in cmd and "--append-system-prompt" in cmd
    assert procs[0].kwargs["cwd"] == str(ws.root.resolve())
    assert "Weekly email summary" in procs[0].stdin_text and "Ada" in procs[0].stdin_text
    assert procs[0].stdin_text.endswith("The person says: How's it going?")
    # Read-only: no Bash for Jef's own chat.
    allowed = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
    assert allowed == ["Read", "Glob", "Grep"]


def test_later_messages_resume_the_same_session(env):
    ws, chats, procs = env
    jefchat.send(chats, ws, "Hi")
    with pytest.raises(ChatError, match="still working"):
        jefchat.send(chats, ws, "Again")
    procs[0].done = True
    jefchat.send(chats, ws, "What's waiting?")
    assert procs[1].cmd[procs[1].cmd.index("--resume") + 1] == "session-1"
    assert procs[1].stdin_text == "What's waiting?"


def test_jef_chat_is_kept_outside_the_projects(env):
    ws, chats, _ = env
    jefchat.send(chats, ws, "Hi")
    assert ws.project_slugs() == ["weekly-email-summary"]
    assert [a.name for a in load_roster(jefchat.jef_workspace(ws), jefchat.HOME)] == ["jef"]
    assert chats.chat_file(jefchat.HOME, jefchat.NAME).exists()


def test_reset_archives_the_chat_and_starts_fresh_with_the_briefing(env):
    ws, chats, procs = env
    jefchat.send(chats, ws, "Hi")  # still working: reset stops it
    when = lambda: datetime(2026, 11, 11, 9, 30, 0)  # noqa: E731
    agent = jefchat.reset(chats, ws, now=when)
    assert procs[0].done  # the old turn was ended
    assert agent.session == "session-2" and agent.role is Role.JEF
    cmd = procs[1].cmd
    assert cmd[cmd.index("--session-id") + 1] == "session-2" and "--append-system-prompt" in cmd
    assert procs[1].stdin_text == jefchat.context_message(ws)
    archive = chats.chat_file(jefchat.HOME, "jef-20261111-093000")
    assert "The person says: Hi" in archive.read_text()
    assert "The person says" not in chats.chat_file(jefchat.HOME, jefchat.NAME).read_text()
    roster = load_roster(jefchat.jef_workspace(ws), jefchat.HOME)
    assert [(a.name, a.session) for a in roster] == [("jef", "session-2")]
    # Resetting twice in the same second keeps both old conversations.
    jefchat.reset(chats, ws, now=when)
    assert chats.chat_file(jefchat.HOME, "jef-20261111-093000-2").exists()
    assert archive.exists()


def test_reset_before_any_chat_just_starts_one(env):
    ws, chats, procs = env
    agent = jefchat.reset(chats, ws)
    assert agent.session == "session-1" and len(procs) == 1
    assert procs[0].stdin_text == jefchat.context_message(ws)


# ---- the home page


@pytest.fixture
def client(env):
    ws, chats, procs = env
    app = create_app(ws, github_check=lambda: GitHubStatus(True, "octocat"), jef_chats=chats)
    return TestClient(app), ws, chats, procs


def test_home_page_shows_the_backlog_before_the_chat(client):
    c, _, _, _ = client
    page = c.get("/").text
    assert "Chat with Jef" in page and 'action="/jef/message"' in page
    assert page.index('id="usage"') < page.index("<h2>Backlog</h2>")
    assert page.index("<h2>Backlog</h2>") < page.index("Chat with Jef")
    assert page.index("Chat with Jef") < page.index('id="projects"')
    assert "No messages yet" in page


def test_sending_a_message_shows_it_and_refreshes_while_working(client):
    c, _, chats, procs = client
    response = c.post("/jef/message", data={"message": "What's going on?"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/#jef"
    page = c.get("/").text
    assert "The person says: What&#39;s going on?" in page
    assert 'http-equiv="refresh"' in page
    log = chats.chat_file(jefchat.HOME, jefchat.NAME)
    with log.open("a") as out:
        out.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "All good."}]}}) + "\n")
    procs[0].done = True
    page = c.get("/").text
    assert "All good." in page and 'http-equiv="refresh"' not in page
    # The Jef chat never shows up in the Projects table.
    assert "/projects/home" not in page


def test_empty_and_busy_messages_show_errors(client):
    c, _, _, _ = client
    assert "Type a message first" in c.post("/jef/message", data={"message": "  "}).text
    c.post("/jef/message", data={"message": "Hi"})
    assert "Jef is still working" in c.post("/jef/message", data={"message": "Again"}).text


def test_reset_button_clears_the_chat_and_starts_over(client):
    c, ws, chats, procs = client
    assert 'action="/jef/reset"' in c.get("/").text
    c.post("/jef/message", data={"message": "Remember the old days"})
    procs[0].done = True
    response = c.post("/jef/reset", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/#jef"
    page = c.get("/").text
    assert "Remember the old days" not in page
    assert "Weekly email summary (In progress, coordinator Ada)" in page  # the new briefing
    assert 'http-equiv="refresh"' in page  # Jef is working on its greeting
    assert len(list(chats.chat_file(jefchat.HOME, jefchat.NAME).parent.glob("jef-*.jsonl"))) == 1


def test_no_claude_means_no_jef_chat(env):
    ws, _, _ = env
    c = TestClient(create_app(ws, github_check=lambda: GitHubStatus(True, "o")))
    page = c.get("/").text
    assert 'action="/jef/message"' not in page and "starts once Claude Code works" in page
    response = c.post("/jef/message", data={"message": "Hi"})
    assert "Claude Code isn't working right now" in response.text
    assert "Claude Code isn't working right now" in c.post("/jef/reset").text
