import json

import pytest

from jef import actions
from jef.actions import ActionError
from jef.backlog import Backlog, BacklogStatus
from jef.chats import ChatError, Chats
from jef.projects import StartError
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
    ws = Workspace(tmp_path / "jef")
    procs = []

    def popen(cmd, **kwargs):
        procs.append(FakeProc(cmd, **kwargs))
        return procs[-1]

    def clone(repo, dest):
        dest.mkdir(parents=True)

    return ws, Chats(ws, "/bin/claude", popen=popen), clone, procs


def started(env, feature="Weekly email summary"):
    """A project started from a new backlog item, its first turn finished."""
    ws, chats, clone, procs = env
    item = actions.add_backlog_item(ws, feature)
    project = actions.start_backlog_item(ws, item.number, "gregyu219/Jef", chats, clone=clone, choose_name=min)
    procs[-1].done = True
    chats.refresh(project.slug)
    return project


def test_add_backlog_item_saves_it(env):
    ws = env[0]
    item = actions.add_backlog_item(ws, "Dark mode")
    assert (item.number, item.status) == (1, BacklogStatus.WAITING)
    assert actions.find_backlog_item(ws, 1).feature == "Dark mode"
    assert actions.find_backlog_item(ws, 2) is None
    with pytest.raises(ValueError):
        actions.add_backlog_item(ws, "  ")
    assert len(Backlog.load(ws.backlog_file).items) == 1


def test_start_backlog_item_needs_claude(env):
    ws, _, clone, _ = env
    actions.add_backlog_item(ws, "Dark mode")
    with pytest.raises(StartError, match="Claude Code isn't working"):
        actions.start_backlog_item(ws, 1, "gregyu219/Jef", None, clone=clone)
    assert ws.project_slugs() == []


def test_start_backlog_item_and_find_its_coordinator(env):
    ws = env[0]
    project = started(env)
    assert project.coordinator == "Ada"
    assert actions.coordinators(ws) == {"Ada": project.slug}
    assert actions.find_coordinator(ws, " ada ").slug == project.slug
    assert actions.find_coordinator(ws, "Bob") is None
    assert actions.known_project(ws, project.slug).coordinator == "Ada"
    assert actions.known_project(ws, "nope") is None
    assert actions.backlog_projects(ws) == {1: project.slug}


def test_repo_choice_offers_known_repos(env):
    ws = env[0]
    assert actions.repo_choice(ws) == actions.RepoChoice("", [], [])
    project = started(env)
    choice = actions.repo_choice(ws)
    assert choice.repo == "gregyu219/Jef"
    assert choice.known == ["gregyu219/Jef"]
    assert [p.slug for p, _ in choice.others] == [project.slug]
    assert actions.repo_choice(ws, "not a repo").others == []


def test_message_coordinator_records_the_sender(env):
    ws, chats, _, procs = env
    project = started(env)
    actions.message_coordinator(ws, chats, project.slug, "  go ahead  ", sender="telegram")
    lines = [json.loads(line) for line in chats.chat_file(project.slug, "ada").read_text().splitlines()]
    sent = [line for line in lines if line.get("type") == "jef_sent"]
    assert sent[-1] == {"type": "jef_sent", "from": "telegram", "text": "go ahead"}
    # Busy now: the chat's own error comes through, with a reason to show.
    with pytest.raises(ChatError) as err:
        actions.message_coordinator(ws, chats, project.slug, "again")
    assert actions.chat_error_reason(err.value) == "busy"


def test_message_coordinator_turns_down_bad_requests(env):
    ws, chats, _, _ = env
    project = started(env)
    for slug, chats_, text, reason in [
        ("nope", chats, "hi", "unknown"),
        (project.slug, chats, "   ", "empty"),
        (project.slug, None, "hi", "claude"),
    ]:
        with pytest.raises(ActionError) as err:
            actions.message_coordinator(ws, chats_, slug, text)
        assert err.value.reason == reason


def test_chat_error_reasons():
    assert actions.chat_error_reason(ChatError("Ada is still working.")) == "busy"
    assert actions.chat_error_reason(ChatError("The budget is used up.")) == "budget"
    assert actions.chat_error_reason(ChatError("Ada has been stopped.")) == "stopped"


def test_summaries_and_statuses_need_no_claude(env):
    ws = env[0]
    project = started(env)
    assert actions.refresh_statuses(ws) >= 0
    [summary] = actions.summarize_projects(ws)
    assert (summary.slug, summary.name, summary.coordinator) == (project.slug, "Weekly email summary", "Ada")
    backlog = Backlog.load(ws.backlog_file)
    assert actions.backlog_auto_approve(ws, backlog)[1][1] is True
    assert isinstance(actions.backlog_attention(ws, backlog), dict)
