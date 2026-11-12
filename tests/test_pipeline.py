import json

import pytest
from fastapi.testclient import TestClient

from jef.backlog import Backlog, BacklogStatus
from jef.chats import Chats, Role, Status, load_roster
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus
from jef.pipeline import Heartbeat, Phase, Pipeline, PipelineError, parse_pr, parse_status, parse_tasks
from jef.publish import PublishError
from jef.projects import goal_fields, slugify, start_project
from jef.status import read_status, status_path
from jef.tasks import TaskStatus
from jef.web.app import ago, create_app
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


class FakePublisher:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.next_pr = 20

    def publish(self, work, branch, base, title, body):
        self.calls.append((branch, base, title, body))
        if self.fail:
            raise PublishError(self.fail)
        url = f"https://github.com/gregyu219/Jef/pull/{self.next_pr}"
        self.next_pr += 1
        return url


READY = "Done.\n```pr\nTitle: Show the version\n\n## What this does\nShows it.\n```\nSTATUS: ready"


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
        self.publisher = FakePublisher()
        self.pipeline = Pipeline(self.ws, self.chats, git=self.git, publisher=self.publisher, pr_state=lambda url: self.pr_states.get(url, "OPEN"), now=lambda: self.clock)
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
    assert parse_status("x\nSTATUS: ready") == ("ready", "")
    assert parse_pr("```pr\nTitle: T\n\nBody here\n```") == ("T", "Body here")
    assert parse_pr("no block") is None


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
    assert "STATUS: ready" in w.last_message("impl-1") and "Don't push" in w.last_message("impl-1")

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

    # Ready: Jef pushes the branch and opens the PR itself. Then it's merged: task 2 starts.
    url = "https://github.com/gregyu219/Jef/pull/20"
    w.reply("impl-1", READY)
    assert w.publisher.calls[0][:3] == (f"jef/{slug}/task-1", "main", "Show the version")
    assert "## What this does" in w.publisher.calls[0][3]
    assert p.tasks(slug).tasks[0].status is TaskStatus.PENDING_REVIEW
    assert p.tasks(slug).tasks[0].pr == url
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
    w.reply("impl-2", READY)
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


def approved_with_one_task(w):
    w.pipeline.start_research(w.slug)
    w.reply("ada", "hi")
    w.reply("research", "findings")
    w.reply("ada", "```tasks\n1. A\n```")
    w.pipeline.approve_tasks(w.slug)


def tasks_waiting(w, block="```tasks\n1. A\n2. B\n```"):
    w.pipeline.start_research(w.slug)
    w.reply("ada", "hi")
    w.reply("research", "findings")
    w.reply("ada", block)
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL


def test_the_coordinator_is_asked_to_decide_once_per_heartbeat(w):
    tasks_waiting(w)
    turns = len(w.procs["ada"])
    w.clock += 59 * 60
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns  # you get a full interval to approve by hand
    w.clock += 60
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns + 1
    message = w.last_message("ada")
    assert "waited about 60 minutes" in message and "ACTION: approve-tasks" in message
    assert "1. A\n2. B" in message
    w.reply("ada", "I'd like to hear from the person first. Is B needed?")
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL
    w.clock += 30 * 60
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns + 1  # not again until the next interval
    w.clock += 30 * 60
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns + 2


def test_the_coordinator_approves_with_the_marker(w):
    tasks_waiting(w)
    w.clock += 3600
    w.pipeline.tick(w.slug)
    w.reply("ada", "The list covers the request.\nACTION: approve-tasks")
    assert w.pipeline.phase(w.slug) is Phase.BUILDING
    assert "impl-1" in w.procs
    assert w.agent("ada").note == "Approved the tasks"


def test_a_new_list_and_the_marker_in_one_reply_does_not_approve(w):
    tasks_waiting(w, "```tasks\n1. A\n```\nACTION: approve-tasks")
    assert "impl-1" not in w.procs
    # A revised list with the marker while waiting is saved but not approved either.
    w.reply("ada", "```tasks\n1. A1\n```\nACTION: approve-tasks")
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL
    assert [t.title for t in w.pipeline.tasks(w.slug).tasks] == ["A1"]


def test_a_revised_list_restarts_the_wait(w):
    tasks_waiting(w)
    turns = len(w.procs["ada"])
    w.clock += 40 * 60
    w.chats.send(w.slug, "ada", "drop B")
    w.reply("ada", "```tasks\n1. A\n```")
    w.clock += 40 * 60
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns + 1  # only your message, no heartbeat yet
    w.clock += 20 * 60
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns + 2
    assert "Heartbeat" in w.last_message("ada")


def test_a_busy_coordinator_is_not_asked_to_decide(w):
    tasks_waiting(w)
    w.chats.send(w.slug, "ada", "what about tests?")
    turns = len(w.procs["ada"])
    w.clock += 3600
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns  # still answering you
    w.reply("ada", "Task 2 adds them.")
    assert len(w.procs["ada"]) == turns + 1
    assert "Heartbeat" in w.last_message("ada")


def test_heartbeat_approval_can_be_turned_off(w):
    assert w.pipeline.coordinator_decides(w.slug)  # on by default
    tasks_waiting(w)
    w.pipeline.set_coordinator_decides(w.slug, False)
    assert goal_fields(w.ws, w.slug)["coordinator decides"] == "no"
    turns = len(w.procs["ada"])
    w.clock += 3 * 3600
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns  # never asked
    # Even if it approves on its own, only you can while it's off.
    w.chats.send(w.slug, "ada", "looks fine to me")
    w.reply("ada", "Great.\nACTION: approve-tasks")
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL
    assert "turned off" in w.agent("ada").note
    # Turning it back on waits a full interval before asking.
    w.pipeline.set_coordinator_decides(w.slug, True)
    turns = len(w.procs["ada"])
    w.clock += 59 * 60
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns
    w.clock += 60
    w.pipeline.tick(w.slug)
    assert "Heartbeat" in w.last_message("ada")
    w.pipeline.approve_tasks(w.slug)  # the button still works either way
    assert w.pipeline.phase(w.slug) is Phase.BUILDING


def test_heartbeat_approval_toggle_on_the_page(w):
    app = create_app(w.ws, github_check=lambda: GitHubStatus(True, "o"), claude_status=ClaudeCheck(True, path="/bin/claude"),
                     chats=w.chats, pipeline=w.pipeline)
    c = TestClient(app)
    tasks_waiting(w)
    page = c.get(f"/projects/{w.slug}").text
    assert "will decide at the next heartbeat" in page and "Turn off" in page
    c.post(f"/projects/{w.slug}/coordinator-decides", data={"on": "no"})
    assert not w.pipeline.coordinator_decides(w.slug)
    page = c.get(f"/projects/{w.slug}").text
    assert "won't approve them for you" in page and "Turn on" in page
    assert "Approve tasks and start building" in page
    c.post(f"/projects/{w.slug}/coordinator-decides", data={"on": "yes"})
    assert w.pipeline.coordinator_decides(w.slug)


def test_check_now_does_not_ask_for_a_decision_early(w):
    tasks_waiting(w)
    turns = len(w.procs["ada"])
    w.clock += 30 * 60
    w.pipeline.check_now(w.slug)
    assert len(w.procs["ada"]) == turns


def test_check_now_notices_a_merge_straight_away_without_nudging(w):
    w.pipeline.start_research(w.slug)
    w.reply("ada", "hi")
    w.reply("research", "findings")
    w.reply("ada", "```tasks\n1. A\n2. B\n```")
    w.pipeline.approve_tasks(w.slug)
    w.reply("impl-1", READY)  # this tick already checked GitHub, so the next waits five minutes
    url = w.pipeline.tasks(w.slug).tasks[0].pr
    w.pr_states[url] = "MERGED"
    w.pipeline.tick(w.slug)
    assert w.pipeline.tasks(w.slug).tasks[0].status is TaskStatus.PENDING_REVIEW
    assert w.pipeline.last_checked(w.slug) is None

    w.pipeline.check_now(w.slug)  # same clock: no waiting
    tasks = w.pipeline.tasks(w.slug).tasks
    assert tasks[0].status is TaskStatus.MERGED
    assert tasks[1].status is TaskStatus.IMPLEMENTATION
    assert w.pipeline.last_checked(w.slug) == w.clock

    # impl-2 is working; pressing the button again doesn't nudge it.
    turns = len(w.procs["impl-2"])
    w.procs["impl-2"][-1].done = True
    w.pipeline.check_now(w.slug)
    assert len(w.procs["impl-2"]) == turns
    assert "Heartbeat check-in" not in w.last_message("impl-2")


def test_check_now_reads_a_ready_reply_and_checks_its_pr_in_one_go(w):
    approved_with_one_task(w)
    url = "https://github.com/gregyu219/Jef/pull/20"
    w.pr_states[url] = "MERGED"
    with w.chats.chat_file(w.slug, "impl-1").open("a") as f:
        f.write(json.dumps({"type": "result", "is_error": False, "result": READY}) + "\n")
    w.procs["impl-1"][-1].done = True
    w.pipeline.check_now(w.slug)
    assert w.pipeline.phase(w.slug) is Phase.DONE


def test_check_now_refuses_finished_projects(w):
    w.chats.stop(w.slug, "ada")
    with pytest.raises(PipelineError):
        w.pipeline.check_now(w.slug)


def test_ago():
    assert ago(5) == "just now"
    assert ago(60) == "1 minute ago"
    assert ago(150) == "2 minutes ago"
    assert ago(2 * 3600 + 5) == "2 hours ago"


def test_web_check_button(w):
    app = create_app(w.ws, github_check=lambda: GitHubStatus(True, "o"), claude_status=ClaudeCheck(True, path="/bin/claude"),
                     chats=w.chats, pipeline=w.pipeline)
    c = TestClient(app)
    page = c.get(f"/projects/{w.slug}").text
    assert "Check status now" in page and "Last checked" not in page
    response = c.post(f"/projects/{w.slug}/check", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/projects/{w.slug}"
    assert "Last checked just now" in c.get(f"/projects/{w.slug}").text
    w.chats.stop(w.slug, "ada")
    page = c.get(f"/projects/{w.slug}").text
    assert "Check status now" not in page
    assert "nothing to check" in c.post(f"/projects/{w.slug}/check").text


def test_publish_problems_go_back_to_the_agent_a_few_times(w):
    approved_with_one_task(w)
    w.publisher.fail = "There are uncommitted changes."
    w.reply("impl-1", READY)
    assert "Jef couldn't open your PR yet: There are uncommitted changes." in w.last_message("impl-1")
    assert w.agent("impl-1").note.startswith("Couldn't open the PR (1)")
    w.reply("impl-1", READY)
    w.reply("impl-1", READY)
    turns = len(w.procs["impl-1"])
    w.reply("impl-1", READY)  # fourth failure: stop sending it back
    assert len(w.procs["impl-1"]) == turns
    assert w.agent("impl-1").note.startswith("Stuck: Jef couldn't open the PR")
    assert w.pipeline.tasks(w.slug).tasks[0].status is TaskStatus.IMPLEMENTATION


def test_ready_without_a_pr_block_is_sent_back(w):
    approved_with_one_task(w)
    w.reply("impl-1", "All done.\nSTATUS: ready")
    assert w.publisher.calls == []
    assert "no ```pr block" in w.last_message("impl-1")


def test_ready_again_updates_the_open_pr(w):
    approved_with_one_task(w)
    w.reply("impl-1", READY)
    w.chats.send(w.slug, "impl-1", "Please rename the variable")
    w.reply("impl-1", "Renamed.\nSTATUS: ready")  # no block needed the second time
    assert len(w.publisher.calls) == 2
    assert w.agent("impl-1").note == "PR updated, waiting for your review"


def test_status_md_follows_the_project_through_every_phase(w):
    beat = Heartbeat(w.pipeline)
    slug = w.slug

    def phase():
        beat.tick_all()
        return read_status(w.ws, slug)["phase"]

    # start_project writes the first one before any tick.
    assert read_status(w.ws, slug)["phase"] == "Understanding"
    assert read_status(w.ws, slug)["coordinator"] == "Ada (Working)"
    w.reply("ada", "Starting.\nACTION: start-research")
    assert phase() == "Research"
    assert read_status(w.ws, slug)["coordinator note"] == "Started research"
    w.reply("research", "# Research\n\nFindings.")
    assert phase() == "Planning"
    w.reply("ada", "```tasks\n1. Show it\n```")
    assert phase() == "Waiting for your approval"
    w.pipeline.approve_tasks(slug)
    assert read_status(w.ws, slug)["phase"] == "Building"  # written straight away
    assert "| 1 | Show it | Implementation | impl-1 | Working | |" in status_path(w.ws, slug).read_text()
    w.reply("impl-1", READY)
    w.pr_states["https://github.com/gregyu219/Jef/pull/20"] = "MERGED"
    w.clock += 301
    assert phase() == "Done"
    fields = read_status(w.ws, slug)
    assert fields["tasks"] == "1 of 1 merged"
    assert fields["coordinator"] == "Ada (Stopped)"


def test_a_stopped_project_still_gets_a_status(w):
    w.chats.stop(w.slug, "ada")
    Heartbeat(w.pipeline).tick_all()  # tick skips Stopped projects; the status is still written
    assert read_status(w.ws, w.slug)["phase"] == "Stopped"


def test_check_now_writes_the_status(w):
    w.reply("ada", "ACTION: start-research")
    status_path(w.ws, w.slug).unlink()
    w.pipeline.check_now(w.slug)
    assert read_status(w.ws, w.slug)["phase"] == "Research"
