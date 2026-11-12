import json

import pytest
from fastapi.testclient import TestClient

from jef.backlog import Backlog, BacklogStatus
from jef.chats import Chats, Role, Status, load_roster
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus
from jef.pipeline import Phase, Pipeline, PipelineError, parse_status, parse_tasks
from jef.projects import goal_fields, slugify, start_project
from jef.tasks import TaskStatus
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


class FakeGit:
    def __init__(self):
        self.worktrees = []
        self.fail = None

    def default_branch(self, repo):
        return "main"

    def new_worktree(self, repo, path, branch, base):
        if self.fail:
            raise PipelineError(self.fail)
        self.worktrees.append((branch, base))
        path.mkdir(parents=True)


class World:
    def __init__(self, tmp_path):
        self.ws = Workspace(tmp_path / "jef")
        backlog = Backlog()
        backlog.add("Show the Jef version number in the page footer")
        backlog.save(self.ws.backlog_file)
        self.procs = {}
        self.pr_states = {}
        self.clock = 1_000_000.0
        self.chats = Chats(self.ws, "/bin/claude", popen=self.popen)
        self.git = FakeGit()
        self.pipeline = Pipeline(self.ws, self.chats, git=self.git, pr_state=lambda url: self.pr_states.get(url, "OPEN"), now=lambda: self.clock)
        project = start_project(self.ws, 1, "gregyu219/Jef", self.chats, clone=lambda repo, dest: dest.mkdir(parents=True), choose_name=lambda names: sorted(names)[0])
        self.slug = project.slug

    def popen(self, cmd, **kwargs):
        proc = FakeProc(cmd, **kwargs)
        name = kwargs["stdout"].name.rsplit("/", 1)[1].removesuffix(".jsonl")
        self.procs.setdefault(name, []).append(proc)
        return proc

    def reply(self, name, text, is_error=False):
        """The agent's current turn ends with this reply."""
        with self.chats.chat_file(self.slug, name).open("a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
            f.write(json.dumps({"type": "result", "is_error": is_error, "result": text}) + "\n")
        self.procs[name][-1].done = True
        self.pipeline.tick(self.slug)

    def last_message(self, name):
        return self.procs[name][-1].stdin_text

    def agent(self, name):
        return next(a for a in load_roster(self.ws, self.slug) if a.name == name)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_parsers():
    assert parse_tasks("Plan:\n```tasks\n1. One\n2) Two\nnot a task\n```") == ["One", "Two"]
    assert parse_tasks("no block") == []
    assert parse_status("Done.\nSTATUS: pr-opened https://github.com/a/b/pull/3") == ("pr-opened", "https://github.com/a/b/pull/3")
    assert parse_status("hmm") is None


def test_project_names_are_short_whole_words():
    assert slugify("Show the Jef version number in the page footer", set()) == "show-jef-version-number-page"
    assert slugify("The", set()) == "the"
    assert slugify("!!!", set()) == "project"


def test_from_go_to_done(w):
    p, slug = w.pipeline, w.slug
    assert p.phase(slug) is Phase.UNDERSTANDING
    w.reply("ada", "I understand. Questions? None. Say go.")
    assert p.phase(slug) is Phase.UNDERSTANDING  # nothing starts without you

    # You say go; Ada confirms with the marker, and a research agent starts.
    w.chats.send(slug, "ada", "go")
    w.reply("ada", "Starting research now.\nACTION: start-research")
    assert p.phase(slug) is Phase.RESEARCH
    research = w.agent("research")
    assert (research.role, research.parent) == (Role.RESEARCH, "ada")
    assert "go" in w.last_message("research")  # it sees your conversation with Ada

    # Research comes back; Jef saves it and asks Ada for tasks.
    w.reply("research", "# Research: footer\n\n## Summary\nAdd it to base.html.")
    assert "## Summary" in (w.ws.project_dir(slug) / "research.md").read_text()
    assert "Add it to base.html" in w.last_message("ada")
    assert goal_fields(w.ws, slug)["planning requested"] == "yes"

    # Ada proposes tasks; they wait for your approval.
    w.reply("ada", "Here's the plan.\n```tasks\n1. Show the version in the footer\n2. Add a test\n```")
    assert p.phase(slug) is Phase.APPROVAL
    assert [t.title for t in p.tasks(slug).tasks] == ["Show the version in the footer", "Add a test"]
    assert "impl-1" not in w.procs

    # You approve: task 1 starts on its own branch. Task 2 waits its turn.
    p.approve_tasks(slug)
    assert p.phase(slug) is Phase.BUILDING
    assert w.git.worktrees == [(f"jef/{slug}/task-1", "main")]
    tasks = p.tasks(slug).tasks
    assert (tasks[0].status, tasks[0].agent) == (TaskStatus.IMPLEMENTATION, "impl-1")
    assert tasks[1].status is TaskStatus.NOT_STARTED
    impl = w.agent("impl-1")
    assert impl.folder.endswith("work/impl-1") and impl.parent == "ada"
    assert "gh pr create --base main" in w.last_message("impl-1")

    # Still working: the heartbeat nudges it once the interval has passed.
    w.reply("impl-1", "Edited the template.\nSTATUS: working tests next")
    assert w.agent("impl-1").note == "Working: tests next"
    count = len(w.procs["impl-1"])
    w.clock += 30 * 60
    p.tick(slug)
    assert len(w.procs["impl-1"]) == count  # not yet
    w.clock += 31 * 60
    p.tick(slug)
    assert "Heartbeat check-in" in w.last_message("impl-1")

    # PR opened, then merged on GitHub: task 2 starts.
    url = "https://github.com/gregyu219/Jef/pull/20"
    w.reply("impl-1", f"Opened it.\nSTATUS: pr-opened {url}")
    assert p.tasks(slug).tasks[0].status is TaskStatus.PENDING_REVIEW
    assert "impl-2" not in w.procs  # one at a time
    w.pr_states[url] = "MERGED"
    w.clock += 301
    p.tick(slug)
    tasks = p.tasks(slug).tasks
    assert tasks[0].status is TaskStatus.MERGED
    assert w.agent("impl-1").status is Status.STOPPED
    assert tasks[1].status is TaskStatus.IMPLEMENTATION

    # Last PR merged: the project and its backlog item are done, agents stopped.
    url2 = "https://github.com/gregyu219/Jef/pull/21"
    w.reply("impl-2", f"STATUS: pr-opened {url2}")
    w.pr_states[url2] = "MERGED"
    w.clock += 301
    p.tick(slug)
    assert p.phase(slug) is Phase.DONE
    assert Backlog.load(w.ws.backlog_file).items[0].status is BacklogStatus.DONE
    assert all(a.status is Status.STOPPED for a in load_roster(w.ws, slug))


def test_replies_are_acted_on_once(w):
    w.reply("ada", "ACTION: start-research")
    w.pipeline.tick(w.slug)
    w.pipeline.tick(w.slug)
    assert len(w.procs["research"]) == 1
    assert w.agent("ada").handled == 1


def test_the_research_marker_only_counts_while_understanding(w):
    w.pipeline.start_research(w.slug)
    w.reply("ada", "ACTION: start-research")
    assert len(w.procs["research"]) == 1
    with pytest.raises(PipelineError):
        w.pipeline.start_research(w.slug)


def test_planning_waits_until_the_coordinator_is_free(w):
    w.pipeline.start_research(w.slug)  # Ada is still on her first turn
    w.reply("research", "# Research\nfindings")
    assert goal_fields(w.ws, w.slug).get("planning requested") != "yes"
    w.reply("ada", "Hello!")
    assert goal_fields(w.ws, w.slug)["planning requested"] == "yes"
    assert "findings" in w.last_message("ada")


def test_tasks_can_be_revised_before_approval_but_not_after(w):
    w.pipeline.start_research(w.slug)
    w.reply("ada", "hi")
    w.reply("research", "findings")
    w.reply("ada", "```tasks\n1. A\n```")
    w.chats.send(w.slug, "ada", "split it in two")
    w.reply("ada", "```tasks\n1. A1\n2. A2\n```")
    assert [t.title for t in w.pipeline.tasks(w.slug).tasks] == ["A1", "A2"]
    w.pipeline.approve_tasks(w.slug)
    w.chats.send(w.slug, "ada", "change it again")
    w.reply("ada", "```tasks\n1. Something else\n```")
    assert [t.title for t in w.pipeline.tasks(w.slug).tasks] == ["A1", "A2"]


def test_stuck_implementers_are_not_nudged_and_errors_are_shown(w):
    w.pipeline.start_research(w.slug)
    w.reply("ada", "hi")
    w.reply("research", "findings")
    w.reply("ada", "```tasks\n1. A\n```")
    w.pipeline.approve_tasks(w.slug)
    w.reply("impl-1", "STATUS: stuck which colour should it be?")
    assert w.agent("impl-1").note == "Stuck: which colour should it be?"
    count = len(w.procs["impl-1"])
    w.clock += 2 * 3600
    w.pipeline.tick(w.slug)
    assert len(w.procs["impl-1"]) == count


def test_a_git_failure_becomes_a_problem_you_can_retry(w):
    w.pipeline.start_research(w.slug)
    w.reply("ada", "hi")
    w.reply("research", "findings")
    w.reply("ada", "```tasks\n1. A\n```")
    w.git.fail = "git fetch failed: network down"
    w.pipeline.approve_tasks(w.slug)
    assert "network down" in w.pipeline.problem(w.slug)
    assert "impl-1" not in w.procs
    w.pipeline.tick(w.slug)  # doesn't keep retrying by itself
    assert "impl-1" not in w.procs
    w.git.fail = None
    w.pipeline.clear_problem(w.slug)
    assert w.pipeline.problem(w.slug) == ""
    assert "impl-1" in w.procs


def test_heartbeat_interval_is_per_project(w):
    assert w.pipeline.heartbeat_minutes(w.slug) == 60
    w.pipeline.set_heartbeat_minutes(w.slug, 15)
    assert w.pipeline.heartbeat_minutes(w.slug) == 15
    with pytest.raises(PipelineError):
        w.pipeline.set_heartbeat_minutes(w.slug, 1)


def test_web_buttons(w):
    app = create_app(w.ws, github_check=lambda: GitHubStatus(True, "o"), claude_status=ClaudeCheck(True, path="/bin/claude"),
                     chats=w.chats, pipeline=w.pipeline)
    c = TestClient(app)
    page = c.get(f"/projects/{w.slug}").text
    assert "Start research" in page
    w.procs["ada"][-1].done = True
    c.post(f"/projects/{w.slug}/research")
    assert w.pipeline.phase(w.slug) is Phase.RESEARCH
    again = c.post(f"/projects/{w.slug}/research").text
    assert "already started" in again
    w.reply("research", "# Research\nfindings here")
    w.reply("ada", "```tasks\n1. Show the version\n```")
    page = c.get(f"/projects/{w.slug}").text
    assert "Approve tasks and start building" in page and "findings here" in page
    c.post(f"/projects/{w.slug}/approve")
    assert "impl-1" in w.procs
    assert "/agents/impl-1" in c.get(f"/projects/{w.slug}").text
    assert "Show the version" in c.get(f"/projects/{w.slug}/agents/impl-1").text
    c.post(f"/projects/{w.slug}/heartbeat", data={"minutes": "30"})
    assert w.pipeline.heartbeat_minutes(w.slug) == 30
    assert "between 5 minutes" in c.post(f"/projects/{w.slug}/heartbeat", data={"minutes": "2"}).text
