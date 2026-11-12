import pytest

from jef.backlog import Backlog, BacklogStatus
from jef.tasks import TaskList, TaskStatus

BACKLOG = """# Backlog

| # | Feature | Status | Coordinator |
| --- | --- | --- | --- |
| 1 | To-do list app | In progress | Ada |
| 2 | Dark mode for the to-do app | Waiting | |
"""

TASKS = """# Tasks: To-do list app

Plan: research.md

| # | Task | Status | Agent | PR |
| --- | --- | --- | --- | --- |
| 1 | Set up the project | Merged | impl-1 | #1 |
| 2 | Show the task list | Pending review | impl-2 | #2 |
| 3 | Save tasks to a file | Implementation | impl-3 | |
| 4 | Add due dates | Research | research-4 | |
| 5 | Dark mode | Not started | | |
"""


def test_backlog_loads_the_documented_format(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    assert [i.feature for i in backlog.items] == ["To-do list app", "Dark mode for the to-do app"]
    assert backlog.items[0].status is BacklogStatus.IN_PROGRESS
    assert backlog.items[0].coordinator == "Ada"
    assert backlog.items[1].status is BacklogStatus.WAITING


def test_backlog_round_trip_is_unchanged(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    assert Backlog.load(path).render() == BACKLOG


def test_removing_a_waiting_item_leaves_the_rest_unchanged(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    removed = backlog.remove(2)
    assert removed.feature == "Dark mode for the to-do app"
    backlog.save(path)
    assert path.read_text() == BACKLOG.replace("| 2 | Dark mode for the to-do app | Waiting | |\n", "")


@pytest.mark.parametrize("number", [1, 99])
def test_started_or_missing_items_cannot_be_removed(tmp_path, number):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    with pytest.raises(ValueError):
        backlog.remove(number)
    assert backlog.render() == BACKLOG


def test_new_backlog_items_start_waiting_with_the_next_number(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    item = backlog.add("  Weekly email summary  ")
    assert (item.number, item.feature, item.status) == (3, "Weekly email summary", BacklogStatus.WAITING)
    backlog.save(path)
    assert "| 3 | Weekly email summary | Waiting | |" in path.read_text()


def test_missing_backlog_file_is_empty_and_can_be_created(tmp_path):
    path = tmp_path / "nested" / "backlog.md"
    backlog = Backlog.load(path)
    assert backlog.items == []
    backlog.add("First idea")
    backlog.save(path)
    assert path.read_text().startswith("# Backlog\n")
    assert Backlog.load(path).items[0].feature == "First idea"


def test_blank_backlog_items_are_rejected():
    with pytest.raises(ValueError):
        Backlog().add("   ")


def test_task_list_loads_all_five_statuses(tmp_path):
    path = tmp_path / "tasks.md"
    path.write_text(TASKS)
    tasks = TaskList.load(path)
    assert tasks.title == "To-do list app"
    assert [t.status for t in tasks.tasks] == list(reversed(TaskStatus))
    assert tasks.tasks[0].pr == "#1"
    assert tasks.counts()[TaskStatus.MERGED] == 1


def test_task_list_round_trip_keeps_the_plan_line(tmp_path):
    path = tmp_path / "tasks.md"
    path.write_text(TASKS)
    assert TaskList.load(path).render() == TASKS


def test_status_names_are_case_insensitive():
    assert TaskStatus.parse("pending REVIEW") is TaskStatus.PENDING_REVIEW
    with pytest.raises(ValueError):
        TaskStatus.parse("Almost done")
