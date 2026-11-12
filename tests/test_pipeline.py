import argparse
import json
import threading

import pytest
from fastapi.testclient import TestClient

from jef.backlog import Backlog, BacklogStatus
from jef.chats import Chats, ChatError, Role, Status, load_roster
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus
from jef import coordinate, inbox, models
from jef.pipeline import PR_POLL_SECONDS, Heartbeat, Phase, Pipeline, PipelineError, last_reply, parse_pr, pr_label
from jef.publish import PublishError
from jef.projects import goal_fields, set_goal_field, slugify, start_project
from jef.status import read_status, render_status, status_path
from jef.tasks import TaskStatus
from jef.usage import budget_left
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

    def fetch(self, repo):
        self.fetches = getattr(self, "fetches", 0) + 1


class FakePublisher:
    def __init__(self):
        self.calls = []
        self.comments = []
        self.fail = None
        self.next_pr = 20
        self.open = {}  # branch -> its open PR, reused like the real publisher

    def publish(self, work, branch, base, title, body):
        self.calls.append((branch, base, title, body))
        if self.fail:
            raise PublishError(self.fail)
        if branch in self.open:
            return self.open[branch]
        url = f"https://github.com/gregyu219/Jef/pull/{self.next_pr}"
        self.next_pr += 1
        self.open[branch] = url
        return url

    def comment(self, work, url, text):
        self.comments.append((url, text))


READY = "Done.\n```pr\nTitle: Show the version\n\n## What this does\nShows it.\n```\nSTATUS: ready"


class World:
    def __init__(self, tmp_path):
        self.ws = Workspace(tmp_path / "jef")
        backlog = Backlog()
        backlog.add("Show the Jef version number in the page footer")
        backlog.save(self.ws.backlog_file)
        self.procs = {}
        self.pr_states = {}
        self.mergeable = {}
        self.clock = 1_000_000.0
        self.chats = Chats(self.ws, "/bin/claude", popen=self.popen, budget_left=lambda slug: budget_left(self.ws, slug))
        self.git = FakeGit()
        self.publisher = FakePublisher()
        self.pipeline = Pipeline(self.ws, self.chats, git=self.git, publisher=self.publisher, pr_state=lambda url: self.pr_states.get(url, "OPEN"),
                                 pr_mergeable=lambda url: self.mergeable.get(url, "MERGEABLE"), now=lambda: self.clock)
        project = start_project(self.ws, 1, "gregyu219/Jef", self.chats, clone=lambda repo, dest: dest.mkdir(parents=True), choose_name=lambda names: sorted(names)[0])
        self.slug = project.slug

    def popen(self, cmd, **kwargs):
        proc = FakeProc(cmd, **kwargs)
        name = kwargs["stdout"].name.rsplit("/", 1)[1].removesuffix(".jsonl")
        self.procs.setdefault(name, []).append(proc)
        return proc

    def reply(self, name, text, is_error=False, cost=0.0, subtype=""):
        """The agent's current turn ends with this reply."""
        result = {"type": "result", "is_error": is_error, "result": text, "total_cost_usd": cost}
        if subtype:
            result["subtype"] = subtype
        with self.chats.chat_file(self.slug, name).open("a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
            f.write(json.dumps(result) + "\n")
        self.procs[name][-1].done = True
        self.pipeline.tick(self.slug)

    def last_message(self, name):
        return self.procs[name][-1].stdin_text

    def agent(self, name):
        return next(a for a in load_roster(self.ws, self.slug) if a.name == name)

    def ask(self, command, sender="ada", **args):
        """The coordinator runs one of its jef commands; Jef carries it out."""
        request_id = inbox.submit(self.ws, self.slug, sender, command, args)
        self.pipeline.process_inbox(self.slug)
        return inbox.wait_for_answer(self.ws, self.slug, request_id, 0)

    def ok(self, command, **args):
        answer = self.ask(command, **args)
        assert answer["ok"], answer["message"]
        return answer["message"]

    def refused(self, command, **args):
        answer = self.ask(command, **args)
        assert not answer["ok"], answer["message"]
        return answer["message"]

    def wakes(self):
        """Messages Jef sent Ada, newest last."""
        return [line.text for line in self.chats.transcript(self.slug, "ada") if line.kind == "sent" and line.text.startswith("heartbeat:")]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


PR20 = "https://github.com/gregyu219/Jef/pull/20"
PR21 = "https://github.com/gregyu219/Jef/pull/21"


def flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def research_done(w):
    """Ada has started research, and the research agent has finished."""
    w.reply("ada", "hi")
    w.ok("research")
    w.reply("research", "# Research\n\nAdd it to base.html.")
    assert w.pipeline.phase(w.slug) is Phase.PLANNING


def tasks_waiting(w, titles=("A", "B")):
    research_done(w)
    w.ok("plan", titles=list(titles))
    w.reply("ada", "Here's the plan.")
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL


def building(w, titles=("A", "B")):
    tasks_waiting(w, titles)
    w.pipeline.approve_tasks(w.slug)
    w.pipeline.tick(w.slug)  # Ada hears about it
    w.ok("start", number="1")
    w.reply("ada", "Started task 1.")


def pr_open(w, titles=("A", "B")):
    building(w, titles)
    w.reply("impl-1", READY)  # Ada is woken to read it
    w.ok("publish", number="1")
    w.reply("ada", "Opened the PR.")


def test_parsers():
    assert parse_pr("```pr\nTitle: T\n\nBody here\n```") == ("T", "Body here")
    assert parse_pr("no block") is None
    assert pr_label(PR20) == "PR #20" and pr_label("nope") == "the PR"


def test_project_names_are_short_whole_words():
    assert slugify("Show the Jef version number in the page footer", set()) == "show-jef-version-number-page"
    assert slugify("The", set()) == "the"
    assert slugify("!!!", set()) == "project"


def test_from_go_to_done(w):
    p, slug = w.pipeline, w.slug
    assert p.phase(slug) is Phase.UNDERSTANDING
    w.reply("ada", "I understand. Questions? None. Say go.")
    assert w.wakes() == []  # nothing happens without you

    # You say go; Ada starts research herself.
    w.chats.send(slug, "ada", "go")
    assert "research agent has started" in w.ok("research", brief="Look at base.html")
    assert p.phase(slug) is Phase.RESEARCH
    research = w.agent("research")
    assert (research.role, research.parent) == (Role.RESEARCH, "ada")
    assert "Look at base.html" in w.last_message("research") and "go" in w.last_message("research")
    w.reply("ada", "Research is running.")

    # Research comes back: Jef saves it and wakes Ada straight away.
    w.reply("research", "# Research: footer\n\n## Summary\nAdd it to base.html.")
    assert "## Summary" in (w.ws.project_dir(slug) / "research.md").read_text()
    assert "research agent finished" in w.last_message("ada")
    assert "## Project status" in w.last_message("ada")

    # Ada plans. The list waits for you; Ada can't approve it straight away.
    assert "waits for the person's approval" in w.ok("plan", titles=["Show the version in the footer", "Add a test"])
    assert p.phase(slug) is Phase.APPROVAL
    assert "wait one heartbeat" in w.refused("approve")
    w.reply("ada", "Here's the plan.")

    # You approve: Ada hears, and starts task 1. Task 2 has to wait.
    p.approve_tasks(slug)
    assert p.phase(slug) is Phase.BUILDING and "impl-1" not in w.procs
    p.tick(slug)
    assert "The person approved the tasks" in w.last_message("ada")
    w.ok("start", number="1")
    assert w.git.worktrees == [(f"jef/{slug}/task-1", "main")]
    impl = w.agent("impl-1")
    assert impl.folder.endswith("work/impl-1") and impl.parent == "ada" and impl.note == "Started by Ada"
    assert "Your coordinator is Ada" in w.last_message("impl-1")
    assert w.refused("start", number="2") == (
        "Task 1 is still being built, and this project builds 1 task at a time. "
        "Start the next one once its PR is open."
    )
    w.reply("ada", "Task 1 is underway.")

    # impl-1 finishes: Ada is woken, reads it, and has Jef open the PR.
    w.reply("impl-1", READY)
    assert "impl-1 finished a turn" in w.last_message("ada")
    assert "## What this does" in last_reply(w.ws, slug, "impl-1")
    assert w.ok("publish", number="1") == (
        f"Opened PR #20: {PR20}. You can start the next task now, unless it needs this one's code."
    )
    assert w.publisher.calls[0][:3] == (f"jef/{slug}/task-1", "main", "Show the version")
    task = p.tasks(slug).tasks[0]
    assert (task.status, task.pr) == (TaskStatus.PENDING_REVIEW, PR20)
    w.reply("ada", "PR is open.")

    # An hour later the heartbeat wakes Ada, who checks the PR on GitHub.
    turns = len(w.procs["ada"])
    w.clock += 59 * 60
    p.tick(slug)
    assert len(w.procs["ada"]) == turns
    w.clock += 2 * 60
    p.tick(slug)
    assert w.last_message("ada").startswith("Heartbeat:")
    assert "can merge" in w.ok("prs")
    w.pr_states[PR20] = "MERGED"
    assert "merged" in w.ok("prs")
    assert p.tasks(slug).tasks[0].status is TaskStatus.MERGED
    assert w.agent("impl-1").status is Status.STOPPED
    w.ok("start", number="2")
    assert w.agent("impl-2").note == "Started by Ada after PR #20 merged"
    w.reply("ada", "Started task 2.")

    # The last PR merges: the project and its backlog item are done.
    w.reply("impl-2", READY)
    w.ok("publish", number="2")
    w.pr_states[PR21] = "MERGED"
    assert "the project is done" in w.ok("prs")
    assert p.phase(slug) is Phase.DONE
    assert Backlog.load(w.ws.backlog_file).items[0].status is BacklogStatus.DONE
    w.reply("ada", "All done!")
    assert all(a.status is Status.STOPPED for a in load_roster(w.ws, slug))


def test_only_the_coordinator_can_ask(w):
    w.reply("ada", "hi")
    for sender in ("research", "impl-1", "someone"):
        assert "Only the project's coordinator" in w.ask("research", sender=sender)["message"]
    assert w.pipeline.phase(w.slug) is Phase.UNDERSTANDING
    assert "doesn't know the request" in w.refused("merge")


def test_requests_are_answered_once_and_in_order(w):
    w.reply("ada", "hi")
    first = inbox.submit(w.ws, w.slug, "ada", "research", {})
    second = inbox.submit(w.ws, w.slug, "ada", "research", {})
    assert w.pipeline.process_inbox(w.slug) == 2
    assert w.pipeline.process_inbox(w.slug) == 0
    assert inbox.wait_for_answer(w.ws, w.slug, first, 0)["ok"]
    assert "already started" in inbox.wait_for_answer(w.ws, w.slug, second, 0)["message"]


def test_ada_is_woken_once_per_finished_turn_and_never_while_busy(w):
    building(w)
    w.chats.send(w.slug, "ada", "how's it going?")  # Ada is busy talking to you
    w.reply("impl-1", "Halfway.\nSTATUS: working tests next")
    assert w.last_message("ada") == "how's it going?"  # not woken mid-turn
    w.reply("ada", "Fine.")  # once Ada is free, she hears about impl-1
    assert "impl-1 finished a turn" in w.last_message("ada")
    turns = len(w.procs["ada"])
    w.procs["ada"][-1].done = True
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns  # only once


def test_no_heartbeat_while_understanding_or_waiting_for_you(w):
    w.reply("ada", "hi")
    w.clock += 10 * 3600
    w.pipeline.tick(w.slug)
    assert w.wakes() == []
    building(w)
    w.ok("wait", reason="which colour?")
    w.reply("ada", "Asked.")
    turns = len(w.procs["ada"])
    w.clock += 10 * 3600
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns


def test_wait_shows_on_the_page_and_try_again_tells_ada(w):
    building(w)
    assert "waiting" in w.ok("wait", reason="Should B use the new API?")
    assert w.pipeline.problem(w.slug) == "Ada is waiting for you: Should B use the new API?"
    w.reply("ada", "I asked.")
    w.pipeline.clear_problem(w.slug)
    assert "pressed Try again on: Ada is waiting for you" in w.last_message("ada")


def test_starting_a_task_lifts_a_wait(w):
    pr_open(w)
    w.pr_states[PR20] = "MERGED"
    w.ok("prs")
    w.ok("wait", reason="check B first")
    w.ok("start", number="2")
    assert w.pipeline.problem(w.slug) == ""


def test_the_coordinator_approves_after_a_heartbeat_if_allowed(w):
    tasks_waiting(w)
    assert "wait one heartbeat" in w.refused("approve")
    w.clock += 61 * 60
    w.pipeline.tick(w.slug)
    assert w.last_message("ada").startswith("Heartbeat:")  # it's asked to look at the list
    w.pipeline.set_coordinator_decides(w.slug, False)
    assert "turned off coordinator approval" in w.refused("approve")
    w.pipeline.set_coordinator_decides(w.slug, True)
    w.ok("approve")
    assert w.pipeline.phase(w.slug) is Phase.BUILDING


def test_no_approval_heartbeat_when_its_turned_off(w):
    tasks_waiting(w)
    w.pipeline.set_coordinator_decides(w.slug, False)
    turns = len(w.procs["ada"])
    w.clock += 3 * 3600
    w.pipeline.tick(w.slug)
    assert len(w.procs["ada"]) == turns


def test_auto_approve_approves_once_the_coordinator_is_idle_and_wakes_it(w):
    w.pipeline.set_auto_approve(w.slug, True)
    assert w.pipeline.auto_approve(w.slug)
    research_done(w)
    w.ok("plan", titles=["A", "B"])
    w.pipeline.tick(w.slug)
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL  # not while Ada is still working
    turns = len(w.procs["ada"])
    w.reply("ada", "Here's the plan.")
    assert w.pipeline.phase(w.slug) is Phase.BUILDING
    assert len(w.procs["ada"]) == turns + 1  # woken straight away
    assert "Jef auto-approved the task list" in w.last_message("ada")
    assert "jef task start 1" in w.last_message("ada")


def test_auto_approve_waits_while_there_is_a_problem(w):
    w.pipeline.set_auto_approve(w.slug, True)
    research_done(w)
    w.ok("plan", titles=["A"])
    set_goal_field(w.ws, w.slug, "Problem", "Something broke")
    w.reply("ada", "Here's the plan.")
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL
    w.pipeline.clear_problem(w.slug)
    assert w.pipeline.phase(w.slug) is Phase.BUILDING


def test_auto_approve_is_off_by_default(w):
    assert goal_fields(w.ws, w.slug)["auto approve"] == "no"
    assert not w.pipeline.auto_approve(w.slug)
    tasks_waiting(w)
    w.pipeline.tick(w.slug)
    assert w.pipeline.phase(w.slug) is Phase.APPROVAL
    assert "- Auto approve: off" in render_status(w.ws, w.slug, load_roster(w.ws, w.slug))
    w.pipeline.set_auto_approve(w.slug, True)
    assert "- Auto approve: on" in render_status(w.ws, w.slug, load_roster(w.ws, w.slug))


def test_plans_can_change_until_building_and_then_only_what_has_not_started(w):
    research_done(w)
    w.ok("plan", titles=["A"])
    w.ok("plan", titles=["A1", "A2"])
    assert [t.title for t in w.pipeline.tasks(w.slug).tasks] == ["A1", "A2"]
    w.pipeline.approve_tasks(w.slug)
    w.ok("start", number="1")
    w.ok("plan", titles=["B, reworded", "C"])
    tasks = w.pipeline.tasks(w.slug).tasks
    assert [(t.number, t.title, t.status) for t in tasks] == [
        (1, "A1", TaskStatus.IMPLEMENTATION), (2, "B, reworded", TaskStatus.NOT_STARTED), (3, "C", TaskStatus.NOT_STARTED),
    ]
    w.ok("plan", titles=[])  # drop the rest
    assert [t.number for t in w.pipeline.tasks(w.slug).tasks] == [1]


def test_bad_requests_are_refused_plainly(w):
    w.reply("ada", "hi")
    assert "nothing to plan from" in w.refused("plan", titles=["A"])
    assert "haven't been approved" in w.refused("start", number="1")
    assert "isn't one of your agents" in w.refused("send", agent="nobody", message="hi")
    building(w)
    assert "Give the task's number" in w.refused("start", number="two")
    assert "no task 9" in w.refused("start", number="9")
    assert "already started" in w.refused("start", number="1")
    assert "still working" in w.refused("publish", number="1")
    assert "still working on its last message" in w.refused("send", agent="impl-1", message="faster")
    assert "isn't one of your agents" in w.refused("stop", agent="ada")


def test_ada_nudges_and_stops_her_agents(w):
    building(w)
    w.reply("impl-1", "Halfway.\nSTATUS: working")
    w.ok("send", agent="impl-1", message="Carry on with the tests, please.")
    assert w.last_message("impl-1") == "Carry on with the tests, please."
    assert any(line.text == "Ada: Carry on with the tests, please." for line in w.chats.transcript(w.slug, "impl-1"))
    w.ok("stop", agent="impl-1")
    assert w.agent("impl-1").status is Status.STOPPED


def test_publish_needs_a_pr_block_and_passes_on_problems(w):
    building(w)
    w.reply("impl-1", "Done.\nSTATUS: ready")
    assert "hasn't written a ```pr block" in w.refused("publish", number="1")
    w.ok("send", agent="impl-1", message="Add the pr block")
    w.reply("impl-1", READY)
    w.publisher.fail = "There are uncommitted changes."
    assert w.refused("publish", number="1") == "There are uncommitted changes."
    w.publisher.fail = None
    w.ok("publish", number="1")
    assert w.agent("impl-1").note == "PR opened, waiting for your review"


def test_a_conflict_is_reported_and_the_fix_goes_to_the_same_pr(w):
    pr_open(w)
    w.mergeable[PR20] = "CONFLICTING"
    assert "has merge conflicts" in w.ok("prs")
    w.ok("send", agent="impl-1", message="Rebase on main and fix the conflicts.")
    w.reply("impl-1", READY)
    said = w.ok("publish", number="1", note="Rebased on main to fix merge conflicts.")
    assert said.startswith(f"Updated PR #20: {PR20}") and "left your note" in said
    assert w.publisher.comments == [(PR20, "Jef: Rebased on main to fix merge conflicts.")]
    assert w.agent("impl-1").note == "PR updated, waiting for your review"
    w.mergeable[PR20] = "UNKNOWN"
    assert "still working out" in w.ok("prs")


def test_a_new_pr_links_the_closed_one(w):
    pr_open(w)
    w.pr_states[PR20] = "CLOSED"
    assert "closed without merging" in w.ok("prs")
    w.publisher.open.clear()  # GitHub has no open PR for the branch any more
    w.ok("send", agent="impl-1", message="The PR was closed; let's open it again")
    w.reply("impl-1", READY)
    assert w.ok("publish", number="1").startswith(f"Opened PR #21: {PR21}")
    assert w.publisher.calls[-1][3].startswith(f"Replaces {PR20}, which was closed without merging.")


def counting_pr_state(w):
    """Swap in a pr_state that records which PRs Jef asked GitHub about."""
    asked = []

    def pr_state(url):
        asked.append(url)
        return w.pr_states.get(url, "OPEN")

    w.pipeline.pr_state = pr_state
    return asked


def test_jefs_timer_spots_a_merge_and_wakes_the_coordinator(w):
    p, slug = w.pipeline, w.slug
    pr_open(w)
    asked = counting_pr_state(w)
    beat = Heartbeat(p)
    beat.tick_all()
    assert asked == [PR20]  # still open: nothing changes, Ada isn't woken
    assert p.tasks(slug).tasks[0].status is TaskStatus.PENDING_REVIEW
    turns = len(w.procs["ada"])

    w.pr_states[PR20] = "MERGED"
    w.clock += PR_POLL_SECONDS - 1
    beat.tick_all()
    assert asked == [PR20] and len(w.procs["ada"]) == turns  # not time to ask yet

    w.clock += 1
    beat.tick_all()
    assert asked == [PR20, PR20]
    assert p.tasks(slug).tasks[0].status is TaskStatus.MERGED
    assert goal_fields(w.ws, slug)["last merged"] == PR20
    assert w.agent("impl-1").status is Status.STOPPED
    assert "PR #20 merged (task 1: A)" in w.last_message("ada")
    assert "start the next task" in w.last_message("ada")
    assert "| 1 | A | Merged |" in status_path(w.ws, slug).read_text()
    w.ok("start", number="2")
    assert w.agent("impl-2").note == "Started by Ada after PR #20 merged"


def test_jefs_timer_only_asks_about_prs_waiting_for_review(w):
    p, slug = w.pipeline, w.slug
    pr_open(w, titles=("A", "B", "C"))
    tasks = p.tasks(slug)
    tasks.tasks[1].status, tasks.tasks[1].pr = TaskStatus.IMPLEMENTATION, PR21
    tasks.tasks[2].status, tasks.tasks[2].pr = TaskStatus.MERGED, "https://github.com/gregyu219/Jef/pull/9"
    p._save_tasks(slug, tasks)
    asked = counting_pr_state(w)
    p.poll_merges(slug)
    assert asked == [PR20]


def test_jefs_timer_changes_nothing_without_a_merge(w):
    p, slug = w.pipeline, w.slug
    pr_open(w)
    turns = len(w.procs["ada"])
    for answer in ("", "CLOSED", "OPEN"):
        w.pr_states[PR20] = answer
        assert p.poll_merges(slug) == []
        p.tick(slug)
        w.clock += PR_POLL_SECONDS
    assert p.tasks(slug).tasks[0].status is TaskStatus.PENDING_REVIEW
    assert "last merged" not in goal_fields(w.ws, slug)
    assert w.agent("impl-1").status is not Status.STOPPED
    assert len(w.procs["ada"]) == turns


def test_jefs_timer_finishes_the_project_on_the_last_merge(w):
    p, slug = w.pipeline, w.slug
    pr_open(w, titles=("A",))
    w.pr_states[PR20] = "MERGED"
    assert p.poll_merges(slug) == ["PR #20 merged (task 1: A). Jef recorded it and stopped impl-1; start the next task."]
    assert p.phase(slug) is Phase.DONE
    assert Backlog.load(w.ws.backlog_file).items[0].status is BacklogStatus.DONE
    assert "1 of 1 merged" in status_path(w.ws, slug).read_text()


def test_a_merge_seen_by_both_checks_is_recorded_once(w):
    p, slug = w.pipeline, w.slug
    pr_open(w)
    w.pr_states[PR20] = "MERGED"
    assert "merged" in w.ok("prs")
    assert p.poll_merges(slug) == []  # already Merged, so not asked or announced again
    w.ok("start", number="2")
    w.reply("impl-2", READY)
    w.ok("publish", number="2")
    w.clock += PR_POLL_SECONDS

    def raced(url):  # `jef prs check` records the merge while Jef is asking
        tasks = p.tasks(slug)
        tasks.tasks[1].status = TaskStatus.MERGED
        p._save_tasks(slug, tasks)
        return "MERGED"

    p.pr_state = raced
    assert p.poll_merges(slug) == []
    assert p.tasks(slug).tasks[1].status is TaskStatus.MERGED


def test_a_spent_budget_pauses_the_project_until_you_raise_it(w):
    p, slug = w.pipeline, w.slug
    assert p.budget_usd(slug) is None
    assert flag(w.procs["ada"][0].cmd, "--max-budget-usd") is None  # no budget, no cap
    p.set_budget_usd(slug, 1)
    assert goal_fields(w.ws, slug)["budget"] == "1.00 USD"
    w.reply("ada", "hi", cost=0.4)
    w.ok("research")
    assert flag(w.procs["research"][0].cmd, "--max-budget-usd") == "0.60"
    w.reply("research", "findings", cost=0.7)  # $1.10 of $1.00

    # Paused: the findings wait, Ada isn't woken, and you can't message her.
    assert p.problem(slug).startswith("Budget reached: $1.10 of $1.00 used")
    assert not (w.ws.project_dir(slug) / "research.md").exists()
    ada_turns = len(w.procs["ada"])
    w.clock += 3 * 3600
    p.tick(slug)
    assert len(w.procs["ada"]) == ada_turns
    with pytest.raises(ChatError, match="budget is used up"):
        w.chats.send(slug, "ada", "hello?")
    p.clear_problem(slug)  # Try again doesn't help while it's still spent
    assert p.problem(slug).startswith("Budget reached")

    # Raising the budget lifts the pause; the next turn is capped at what's left.
    p.set_budget_usd(slug, 5)
    assert p.problem(slug) == ""
    p.tick(slug)
    assert (w.ws.project_dir(slug) / "research.md").exists()
    assert len(w.procs["ada"]) == ada_turns + 1
    assert flag(w.procs["ada"][-1].cmd, "--max-budget-usd") == "3.90"

    p.set_budget_usd(slug, None)
    assert goal_fields(w.ws, slug)["budget"] == "none" and p.budget_usd(slug) is None
    for bad in (0, -3, float("nan"), float("inf")):
        with pytest.raises(PipelineError):
            p.set_budget_usd(slug, bad)


def test_heartbeat_interval_is_per_project(w):
    w.pipeline.set_heartbeat_minutes(w.slug, 15)
    assert w.pipeline.heartbeat_minutes(w.slug) == 15
    assert goal_fields(w.ws, w.slug)["heartbeat"] == "15 minutes"
    with pytest.raises(PipelineError):
        w.pipeline.set_heartbeat_minutes(w.slug, 1)


def test_implementers_at_once_is_per_project(w):
    p = w.pipeline
    assert p.implementers_at_once(w.slug) == 1
    p.set_implementers_at_once(w.slug, 3)
    assert p.implementers_at_once(w.slug) == 3
    assert goal_fields(w.ws, w.slug)["implementers at once"] == "3"
    for bad in (0, -1, 6):
        with pytest.raises(PipelineError, match="between 1 and 5"):
            p.set_implementers_at_once(w.slug, bad)
    assert p.implementers_at_once(w.slug) == 3


def test_odd_implementers_at_once_falls_back_to_one(w):
    for odd in ("lots", "0", "9"):
        set_goal_field(w.ws, w.slug, "Implementers at once", odd)
        assert w.pipeline.implementers_at_once(w.slug) == 1


def test_tasks_run_side_by_side_up_to_the_limit(w):
    p, slug = w.pipeline, w.slug
    tasks_waiting(w, ("A", "B", "C"))
    p.set_implementers_at_once(slug, 2)
    p.approve_tasks(slug)
    p.tick(slug)
    w.ok("start", number="1")
    w.ok("start", number="2")
    assert w.git.worktrees == [(f"jef/{slug}/task-1", "main"), (f"jef/{slug}/task-2", "main")]
    assert w.agent("impl-1").status is Status.WORKING and w.agent("impl-2").status is Status.WORKING
    assert w.refused("start", number="3") == (
        "Tasks 1 and 2 are still being built, and this project builds at most 2 tasks at a time. "
        "Start the next one once one of their PRs is open."
    )
    w.reply("ada", "Started tasks 1 and 2.")

    # Both finish while Ada is busy: she hears about both in one wake.
    w.chats.send(slug, "ada", "how's it going?")
    w.reply("impl-1", READY)
    w.reply("impl-2", READY)
    assert w.last_message("ada") == "how's it going?"
    w.reply("ada", "Fine.")
    wake = w.last_message("ada")
    assert "impl-1 finished a turn" in wake and "impl-2 finished a turn" in wake
    assert "- Implementers at once: 2" in wake
    w.ok("publish", number="1")
    w.ok("publish", number="2")

    # PRs waiting for review don't count toward the limit: task 3 can start.
    w.ok("start", number="3")
    assert w.agent("impl-3").note == "Started by Ada"
    assert [t.status for t in p.tasks(slug).tasks] == [
        TaskStatus.PENDING_REVIEW, TaskStatus.PENDING_REVIEW, TaskStatus.IMPLEMENTATION
    ]


def test_the_next_task_starts_once_the_last_pr_is_open(w):
    p, slug = w.pipeline, w.slug
    tasks_waiting(w, ("A", "B", "C"))
    p.approve_tasks(slug)
    p.tick(slug)
    w.ok("start", number="1")
    assert "Task 1 is still being built" in w.refused("start", number="2")
    w.reply("ada", "Started task 1.")
    w.reply("impl-1", READY)
    w.reply("ada", "Reading it.")
    assert "You can start the next task now" in w.ok("publish", number="1")

    # Nothing has merged, and the limit is 1, yet task 2 starts.
    w.ok("start", number="2")
    assert [t.status for t in p.tasks(slug).tasks] == [
        TaskStatus.PENDING_REVIEW, TaskStatus.IMPLEMENTATION, TaskStatus.NOT_STARTED
    ]
    assert "Task 2 is still being built" in w.refused("start", number="3")


def test_overview_shows_implementers_at_once(w):
    w.pipeline.set_implementers_at_once(w.slug, 4)
    w.pipeline.write_status(w.slug)
    assert read_status(w.ws, w.slug)["implementers at once"] == "4"


def test_models_are_per_project(w):
    p = w.pipeline
    assert p.models(w.slug) == {Role.COORDINATOR: "sonnet", Role.RESEARCH: "opus", Role.IMPLEMENTER: "opus"}
    p.set_models(w.slug, {Role.COORDINATOR: "Haiku", Role.IMPLEMENTER: "default"})
    assert p.models(w.slug) == {Role.COORDINATOR: "haiku", Role.RESEARCH: "opus", Role.IMPLEMENTER: "default"}
    assert goal_fields(w.ws, w.slug)["coordinator model"] == "haiku"
    with pytest.raises(PipelineError):
        p.set_models(w.slug, {Role.RESEARCH: "sonnet", Role.COORDINATOR: "gpt-9"})
    assert p.models(w.slug)[Role.RESEARCH] == "opus"  # nothing saved
    with pytest.raises(PipelineError):
        p.set_models(w.slug, {Role.JEF: "opus"})


def test_coordinators_get_jef_commands_and_nobody_else_does(w):
    ada = w.procs["ada"][0]
    assert ada.kwargs["env"]["JEF_PROJECT"] == w.slug and ada.kwargs["env"]["JEF_AGENT"] == "ada"
    assert "Bash(jef task *)" in ada.cmd and "Bash(jef stop)" not in ada.cmd
    assert "jef task start" in flag(ada.cmd, "--append-system-prompt")
    building(w)
    impl = w.procs["impl-1"][0]
    assert "JEF_PROJECT" not in impl.kwargs["env"] and "JEF_AGENT" not in impl.kwargs["env"]
    assert "Bash(jef task *)" not in impl.cmd and "Bash(jef *)" in impl.cmd[impl.cmd.index("--disallowedTools"):]
    # Every turn carries the role's instructions, not just the first.
    assert "jef task start" in flag(w.procs["ada"][-1].cmd, "--append-system-prompt")
    assert "--resume" in w.procs["ada"][-1].cmd


# ---- the jef commands coordinators run


def parse(*argv):
    from jef.cli import build_parser

    return build_parser().parse_args(list(argv))


def coordinator_env(w):
    return {"JEF_PROJECT": w.slug, "JEF_AGENT": "ada"}


def test_jef_project_only_works_in_a_coordinators_turn(w, capsys):
    assert coordinate.run(parse("overview"), ws=w.ws, env={}) == 2
    assert coordinate.run(parse("overview"), ws=w.ws, env={"JEF_PROJECT": w.slug, "JEF_AGENT": "impl-1"}) == 2
    assert "only works inside a coordinator's turn" in capsys.readouterr().out


def test_jef_project_reads_without_jef_running(w, capsys):
    assert coordinate.run(parse("overview"), ws=w.ws, env=coordinator_env(w)) == 0
    assert "- Phase: Understanding" in capsys.readouterr().out
    assert coordinate.run(parse("agent", "read", "ada"), ws=w.ws, env=coordinator_env(w)) == 0
    assert "still working on its first turn" in capsys.readouterr().out
    assert coordinate.run(parse("agent", "read", "nobody"), ws=w.ws, env=coordinator_env(w)) == 1
    # Changing things needs the running Jef.
    assert coordinate.run(parse("research", "start"), ws=w.ws, env=coordinator_env(w)) == 1
    assert "Jef isn't running" in capsys.readouterr().out


def test_jef_project_round_trip_through_the_running_jef(w, capsys):
    w.reply("ada", "hi")
    inbox.mark_alive(w.ws)
    stop = threading.Event()

    def jef():  # the running Jef's inbox loop
        while not stop.is_set():
            w.pipeline.process_inbox(w.slug)
            stop.wait(0.05)

    thread = threading.Thread(target=jef)
    thread.start()
    try:
        code = coordinate.run(parse("research", "start", "look", "at", "base.html"), ws=w.ws, env=coordinator_env(w), wait_seconds=5)
        assert code == 0 and "research agent has started" in capsys.readouterr().out
        assert "look at base.html" in w.last_message("research")
        code = coordinate.run(parse("agent", "send", "impl-9", "hello"), ws=w.ws, env=coordinator_env(w), wait_seconds=5)
        assert code == 1 and "isn't one of your agents" in capsys.readouterr().out
    finally:
        stop.set()
        thread.join()


def test_request_args():
    assert coordinate.request_args(parse("research", "start")) == ("research", {"brief": ""})
    assert coordinate.request_args(parse("tasks", "set", "A", "B")) == ("plan", {"titles": ["A", "B"]})
    assert coordinate.request_args(parse("tasks", "approve")) == ("approve", {})
    assert coordinate.request_args(parse("task", "start", "1")) == ("start", {"number": "1"})
    assert coordinate.request_args(parse("task", "publish", "2", "--note", "rebased")) == ("publish", {"number": "2", "note": "rebased"})
    assert coordinate.request_args(parse("prs", "check")) == ("prs", {})
    assert coordinate.request_args(parse("agent", "send", "impl-2", "carry", "on")) == ("send", {"agent": "impl-2", "message": "carry on"})
    assert coordinate.request_args(parse("agent", "stop", "impl-2")) == ("stop", {"agent": "impl-2"})
    assert coordinate.request_args(parse("wait", "need", "an", "answer")) == ("wait", {"reason": "need an answer"})


def test_your_own_jef_commands_are_unchanged():
    from jef.cli import build_parser

    assert build_parser().parse_args(["status"]).command == "status"
    assert not getattr(build_parser().parse_args(["stop"]), "coordinator", False)
    assert getattr(build_parser().parse_args(["task", "start", "1"]), "coordinator", False)


# ---- the web page


def client(w):
    app = create_app(w.ws, github_check=lambda: GitHubStatus(True, "o"), claude_status=ClaudeCheck(True, path="/bin/claude"),
                     chats=w.chats, pipeline=w.pipeline)
    return TestClient(app)


def test_web_buttons(w):
    c = client(w)
    page = c.get(f"/projects/{w.slug}").text
    assert "Start research" in page
    w.procs["ada"][-1].done = True
    c.post(f"/projects/{w.slug}/research")
    assert w.pipeline.phase(w.slug) is Phase.RESEARCH
    assert "already started" in c.post(f"/projects/{w.slug}/research").text
    w.pipeline.tick(w.slug)
    assert "pressed Start research" in w.last_message("ada")
    w.reply("ada", "ok")
    w.reply("research", "# Research\nfindings here")
    w.ok("plan", titles=["Show the version"])
    w.reply("ada", "plan sent")
    page = c.get(f"/projects/{w.slug}").text
    assert "Approve tasks and start building" in page and "findings here" in page
    c.post(f"/projects/{w.slug}/approve")
    assert w.pipeline.phase(w.slug) is Phase.BUILDING
    w.pipeline.tick(w.slug)
    w.ok("start", number="1")
    assert "/agents/impl-1" in c.get(f"/projects/{w.slug}").text
    assert "Show the version" in c.get(f"/projects/{w.slug}/agents/impl-1").text
    c.post(f"/projects/{w.slug}/heartbeat", data={"minutes": "30"})
    assert w.pipeline.heartbeat_minutes(w.slug) == 30
    assert "between 5 minutes" in c.post(f"/projects/{w.slug}/heartbeat", data={"minutes": "2"}).text


def test_web_implementers_form(w):
    c = client(w)
    page = c.get(f"/projects/{w.slug}").text
    assert 'action="/projects/{}/implementers"'.format(w.slug) in page
    assert 'name="count" value="1"' in page and "Build up to" in page
    c.post(f"/projects/{w.slug}/implementers", data={"count": "3"})
    assert w.pipeline.implementers_at_once(w.slug) == 3
    assert 'name="count" value="3"' in c.get(f"/projects/{w.slug}").text
    assert "Type a number of tasks" in c.post(f"/projects/{w.slug}/implementers", data={"count": "lots"}).text
    assert "between 1 and 5" in c.post(f"/projects/{w.slug}/implementers", data={"count": "0"}).text
    assert "between 1 and 5" in c.post(f"/projects/{w.slug}/implementers", data={"count": "6"}).text
    assert w.pipeline.implementers_at_once(w.slug) == 3


def test_web_page_text_follows_implementers_at_once(w):
    c = client(w)
    building(w)
    assert "starts the next task as soon as a PR is open" in c.get(f"/projects/{w.slug}").text
    w.pipeline.set_implementers_at_once(w.slug, 2)
    page = c.get(f"/projects/{w.slug}").text
    assert "whichever tasks are ready, up to 2 at once" in page


def test_web_wake_button(w):
    c = client(w)
    building(w)
    page = c.get(f"/projects/{w.slug}").text
    assert "Ask Ada to check now" in page
    c.post(f"/projects/{w.slug}/wake")
    assert "asked you to check on the project now" in w.last_message("ada")
    assert "busy right now" in c.post(f"/projects/{w.slug}/wake").text


def test_web_check_button(w):
    c = client(w)
    page = c.get(f"/projects/{w.slug}").text
    assert "Check status now" in page and "Last checked" not in page
    response = c.post(f"/projects/{w.slug}/check", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/projects/{w.slug}"
    assert "Last checked just now" in c.get(f"/projects/{w.slug}").text
    w.chats.stop(w.slug, "ada")
    page = c.get(f"/projects/{w.slug}").text
    assert "Check status now" not in page
    assert "nothing to check" in c.post(f"/projects/{w.slug}/check").text


def test_check_now_costs_nothing_without_news(w):
    building(w)
    turns = len(w.procs["ada"])
    w.clock += 30 * 60
    w.pipeline.check_now(w.slug)
    assert len(w.procs["ada"]) == turns


def test_web_budget_form(w):
    c = client(w)
    assert "No budget" in c.get(f"/projects/{w.slug}").text
    c.post(f"/projects/{w.slug}/budget", data={"amount": "$12.50"})
    assert w.pipeline.budget_usd(w.slug) == 12.5
    w.reply("ada", "hi", cost=2.5)
    page = c.get(f"/projects/{w.slug}").text
    assert "Budget $12.50 · $10.00 left" in page and 'value="12.50"' in page
    assert "Type a dollar amount" in c.post(f"/projects/{w.slug}/budget", data={"amount": "lots"}).text
    assert "above $0" in c.post(f"/projects/{w.slug}/budget", data={"amount": "0"}).text
    c.post(f"/projects/{w.slug}/budget", data={"amount": "2"})
    page = c.post(f"/projects/{w.slug}/message", data={"message": "hello"}).text
    assert "budget is used up" in page and "Budget reached" in page
    c.post(f"/projects/{w.slug}/budget", data={"amount": ""})
    assert w.pipeline.budget_usd(w.slug) is None


def test_web_models_form(w):
    c = client(w)
    w.chats.model_for = lambda slug, role: models.model_for(w.ws, slug, role)
    page = c.get(f"/projects/{w.slug}").text
    assert 'action="/projects/{}/models"'.format(w.slug) in page
    assert '<option value="sonnet" selected>' in page
    c.post(f"/projects/{w.slug}/models", data={"coordinator": "haiku", "research": "fable", "implementer": "opus"})
    assert w.pipeline.models(w.slug) == {Role.COORDINATOR: "haiku", Role.RESEARCH: "fable", Role.IMPLEMENTER: "opus"}
    assert '<option value="haiku" selected>' in c.get(f"/projects/{w.slug}").text
    assert "Pick one of" in c.post(f"/projects/{w.slug}/models", data={"coordinator": "gpt-9"}).text
    # The coordinator's next turn runs on the new model.
    w.reply("ada", "hi")
    c.post(f"/projects/{w.slug}/message", data={"message": "hello"})
    assert flag(w.procs["ada"][-1].cmd, "--model") == "haiku"


def test_ago():
    assert ago(5) == "just now"
    assert ago(60) == "1 minute ago"
    assert ago(150) == "2 minutes ago"
    assert ago(2 * 3600 + 5) == "2 hours ago"


# ---- status.md


def test_status_md_follows_the_project_through_every_phase(w):
    beat = Heartbeat(w.pipeline)
    slug = w.slug

    def phase():
        beat.tick_all()
        return read_status(w.ws, slug)["phase"]

    assert read_status(w.ws, slug)["phase"] == "Understanding"
    assert read_status(w.ws, slug)["coordinator"] == "Ada (Working)"
    w.reply("ada", "hi")
    w.ok("research")
    assert phase() == "Research"
    w.reply("research", "# Research\n\nFindings.")
    assert phase() == "Planning"
    w.ok("plan", titles=["Show it"])
    assert phase() == "Waiting for your approval"
    w.pipeline.approve_tasks(slug)
    assert read_status(w.ws, slug)["phase"] == "Building"  # written straight away
    w.ok("start", number="1")
    assert "| 1 | Show it | Implementation | impl-1 | Working | | Started by Ada |" in status_path(w.ws, slug).read_text()
    w.reply("impl-1", READY)
    w.ok("publish", number="1")
    w.pr_states[PR20] = "MERGED"
    w.ok("prs")
    assert phase() == "Done"
    assert read_status(w.ws, slug)["tasks"] == "1 of 1 merged"
    w.procs["ada"][-1].done = True
    assert phase() == "Done"
    assert read_status(w.ws, slug)["coordinator"] == "Ada (Stopped)"


def test_a_stopped_project_still_gets_a_status(w):
    w.chats.stop(w.slug, "ada")
    Heartbeat(w.pipeline).tick_all()
    assert read_status(w.ws, w.slug)["phase"] == "Stopped"


def test_heartbeat_marks_jef_alive_and_answers_requests(w):
    w.reply("ada", "hi")
    request_id = inbox.submit(w.ws, w.slug, "ada", "research", {})
    Heartbeat(w.pipeline).inbox_all()
    assert inbox.jef_is_running(w.ws)
    assert inbox.wait_for_answer(w.ws, w.slug, request_id, 0)["ok"]
