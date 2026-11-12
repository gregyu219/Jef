from datetime import datetime

import pytest

from jef import mdtable
from jef.chats import Agent, Role, Status
from jef.projects import set_goal_field
from jef.status import (
    NEEDS_APPROVAL,
    NEEDS_PROBLEM,
    NEEDS_QUESTION,
    NEEDS_REVIEW,
    WAITING,
    Attention,
    Phase,
    needs_you,
    project_phase,
    read_status,
    render_status,
    status_path,
    write_status,
)
from jef.tasks import Task, TaskList, TaskStatus
from jef.workspace import Workspace

SLUG = "show-version"
T1 = datetime(2026, 11, 11, 14, 5)
T2 = datetime(2026, 11, 11, 15, 30)


@pytest.fixture
def ws(tmp_path):
    ws = Workspace(tmp_path / "jef")
    folder = ws.project_dir(SLUG)
    folder.mkdir(parents=True)
    (folder / "goal.md").write_text(
        "# Goal: Show the version\n\n- Repo: o/r\n- Coordinator: Ada\n\n## What you asked for\n\nIt.\n",
        encoding="utf-8",
    )
    return ws


def ada(status=Status.IDLE, note=""):
    return Agent("ada", Role.COORDINATOR, "s", "/f", status=status, note=note)


def save_tasks(ws, *tasks):
    TaskList(list(tasks), title="Show the version").save(ws.project_dir(SLUG) / "tasks.md")


def test_phase_comes_from_the_files(ws):
    assert project_phase(ws, SLUG, [ada()]) is Phase.UNDERSTANDING
    set_goal_field(ws, SLUG, "Research", "started")
    assert project_phase(ws, SLUG, [ada()]) is Phase.RESEARCH
    set_goal_field(ws, SLUG, "Research", "done")
    assert project_phase(ws, SLUG, [ada()]) is Phase.PLANNING
    save_tasks(ws, Task(1, "Show it"))
    assert project_phase(ws, SLUG, [ada()]) is Phase.APPROVAL
    set_goal_field(ws, SLUG, "Tasks approved", "yes")
    assert project_phase(ws, SLUG, [ada()]) is Phase.BUILDING
    assert project_phase(ws, SLUG, [ada(Status.STOPPED)]) is Phase.STOPPED
    set_goal_field(ws, SLUG, "Done", "yes")
    assert project_phase(ws, SLUG, [ada(Status.STOPPED)]) is Phase.DONE


def test_render_and_read_round_trip(ws):
    set_goal_field(ws, SLUG, "Tasks approved", "yes")
    save_tasks(
        ws,
        Task(1, "Show it", TaskStatus.MERGED, "impl-1", "https://github.com/o/r/pull/1"),
        Task(2, "Test it", TaskStatus.IMPLEMENTATION, "impl-2"),
        Task(3, "Document it"),
    )
    agents = [
        ada(note="Approved the tasks"),
        Agent("impl-1", Role.IMPLEMENTER, "s1", "/w1", status=Status.STOPPED, note="PR opened"),
        Agent("impl-2", Role.IMPLEMENTER, "s2", "/w2", status=Status.WORKING, note="Working: tests next"),
    ]
    assert write_status(ws, SLUG, agents, T1)
    text = status_path(ws, SLUG).read_text()
    assert text.startswith("# Status: Show the version\n")
    assert read_status(ws, SLUG) == {
        "name": "Show the version",
        "phase": "Building",
        "coordinator": "Ada (Idle)",
        "coordinator note": "Approved the tasks",
        "problem": "none",
        "tasks": "1 of 3 merged",
        "implementers at once": "1",
        "auto approve": "off",
        "updated": "2026-11-11 14:05",
    }
    rows = mdtable.parse(text).rows
    assert rows[0] == ["1", "Show it", "Merged", "impl-1", "Stopped", "https://github.com/o/r/pull/1", "PR opened"]
    assert rows[1][2:5] == ["Implementation", "impl-2", "Working"]
    assert rows[2] == ["3", "Document it", "Not started", "", "", "", ""]


def test_no_tasks_and_no_status_file(ws):
    assert read_status(ws, SLUG) == {}
    text = render_status(ws, SLUG, [], T1)
    assert "- Coordinator: Ada (not started)" in text and "- Tasks: none yet" in text
    assert "No tasks yet." in text and "|" not in text


def test_untrusted_notes_stay_on_one_line_and_in_their_cell(ws):
    save_tasks(ws, Task(1, "Show it", TaskStatus.IMPLEMENTATION, "impl-1"))
    note = "Stuck: a | b\n- Phase: Done\n" + "x" * 500
    agents = [ada(note=note), Agent("impl-1", Role.IMPLEMENTER, "s", "/w", note=note)]
    write_status(ws, SLUG, agents, T1)
    fields = read_status(ws, SLUG)
    assert fields["phase"] == "Waiting for your approval"  # the note can't fake a line
    assert fields["coordinator note"].startswith("Stuck: a | b - Phase: Done")
    assert len(fields["coordinator note"]) <= 160
    row = mdtable.parse(status_path(ws, SLUG).read_text()).rows[0]
    assert len(row) == 7 and row[6].startswith("Stuck: a | b")


def test_unchanged_status_is_not_rewritten(ws):
    assert write_status(ws, SLUG, [ada()], T1)
    assert not write_status(ws, SLUG, [ada()], T2)  # only the time differs
    assert read_status(ws, SLUG)["updated"] == "2026-11-11 14:05"
    assert write_status(ws, SLUG, [ada(Status.WORKING)], T2)
    assert read_status(ws, SLUG)["updated"] == "2026-11-11 15:30"


def test_problem_is_shown(ws):
    set_goal_field(ws, SLUG, "Problem", "Couldn't start task 1: boom")
    assert "- Problem: Couldn't start task 1: boom" in render_status(ws, SLUG, [ada()], T1)


PR = "https://github.com/o/r/pull/34"


def test_needs_you_is_none_while_the_agents_work(ws):
    set_goal_field(ws, SLUG, "Tasks approved", "yes")
    save_tasks(ws, Task(1, "Show it", TaskStatus.IMPLEMENTATION, "impl-1"), Task(2, "Test it"))
    assert needs_you(ws, SLUG, [ada()]) is None


def test_needs_you_for_a_pr_to_review(ws):
    set_goal_field(ws, SLUG, "Tasks approved", "yes")
    save_tasks(ws, Task(1, "Show it", TaskStatus.MERGED, "impl-1", "https://github.com/o/r/pull/1"),
               Task(2, "Test it", TaskStatus.PENDING_REVIEW, "impl-2", PR))
    assert needs_you(ws, SLUG, [ada()]) == Attention(NEEDS_REVIEW, "Test it", link=PR)


def test_needs_you_not_while_the_implementer_fixes_conflicts(ws):
    set_goal_field(ws, SLUG, "Tasks approved", "yes")
    save_tasks(ws, Task(1, "Show it", TaskStatus.PENDING_REVIEW, "impl-1", PR))
    set_goal_field(ws, SLUG, "Conflict", "fixing 1")
    assert needs_you(ws, SLUG, [ada()]) is None
    set_goal_field(ws, SLUG, "Conflict", "asked 1")  # still yours to review
    assert needs_you(ws, SLUG, [ada()]).reason == NEEDS_REVIEW


def test_needs_you_for_a_question_or_a_problem(ws):
    set_goal_field(ws, SLUG, "Tasks approved", "yes")
    save_tasks(ws, Task(1, "Show it", TaskStatus.PENDING_REVIEW, "impl-1", PR))
    set_goal_field(ws, SLUG, "Problem", f"Ada {WAITING}Which colour?")
    assert needs_you(ws, SLUG, [ada()]) == Attention(NEEDS_QUESTION, "Which colour?")
    set_goal_field(ws, SLUG, "Problem", "Couldn't start task 2: boom")
    assert needs_you(ws, SLUG, [ada()]) == Attention(NEEDS_PROBLEM, "Couldn't start task 2: boom")


def test_needs_you_to_approve_tasks(ws):
    save_tasks(ws, Task(1, "Show it"))
    assert needs_you(ws, SLUG, [ada()]) == Attention(NEEDS_APPROVAL)


def test_needs_you_is_none_when_done_or_stopped(ws):
    save_tasks(ws, Task(1, "Show it", TaskStatus.PENDING_REVIEW, "impl-1", PR))
    assert needs_you(ws, SLUG, [ada(Status.STOPPED)]) is None
    set_goal_field(ws, SLUG, "Done", "yes")
    assert needs_you(ws, SLUG, [ada()]) is None
