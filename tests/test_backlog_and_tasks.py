import pytest

from jef.backlog import Backlog, BacklogStatus
from jef.tasks import TaskList, TaskStatus

BACKLOG = """# Backlog

| # | Feature | Status | Coordinator | Auto approve |
| --- | --- | --- | --- | --- |
| 1 | To-do list app | In progress | Ada | |
| 2 | Dark mode for the to-do app | Waiting | | yes |
"""

OLD_BACKLOG = """# Backlog

| # | Feature | Status | Coordinator |
| --- | --- | --- | --- |
| 1 | To-do list app | In progress | Ada |
"""

TASKS = """# Tasks: To-do list app

Plan: research.md

| # | Task | After | Status | Agent | PR |
| --- | --- | --- | --- | --- | --- |
| 1 | Set up the project | | Merged | impl-1 | #1 |
| 2 | Show the task list | 1 | Pending review | impl-2 | #2 |
| 3 | Save tasks to a file | 1 | Implementation | impl-3 | |
| 4 | Add due dates | | Research | research-4 | |
| 5 | Dark mode | 2, 3 | Not started | | |
"""

OLD_TASKS = """# Tasks: To-do list app

| # | Task | Status | Agent | PR |
| --- | --- | --- | --- | --- |
| 1 | Set up the project | Merged | impl-1 | #1 |
| 2 | Dark mode | Not started | | |
"""


def test_backlog_loads_the_documented_format(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    assert [i.feature for i in backlog.items] == ["To-do list app", "Dark mode for the to-do app"]
    assert backlog.items[0].status is BacklogStatus.IN_PROGRESS
    assert backlog.items[0].coordinator == "Ada"
    assert backlog.items[1].status is BacklogStatus.WAITING
    assert [i.auto_approve for i in backlog.items] == [False, True]


def test_backlog_round_trip_is_unchanged(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    assert Backlog.load(path).render() == BACKLOG


def test_old_backlog_without_auto_approve_loads_as_off_and_gains_the_column(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(OLD_BACKLOG)
    backlog = Backlog.load(path)
    assert backlog.items[0].auto_approve is False
    backlog.items[0].auto_approve = True
    assert "| 1 | To-do list app | In progress | Ada | yes |" in backlog.render()


def test_removing_a_waiting_item_leaves_the_rest_unchanged(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    removed = backlog.remove(2)
    assert removed.feature == "Dark mode for the to-do app"
    backlog.save(path)
    assert path.read_text() == BACKLOG.replace("| 2 | Dark mode for the to-do app | Waiting | | yes |\n", "")


WITH_DONE = BACKLOG + "| 3 | Weekly email summary | Done | Bea | |\n"


def test_removing_a_done_item_leaves_the_rest_unchanged(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(WITH_DONE)
    backlog = Backlog.load(path)
    removed = backlog.remove(3)
    assert removed.feature == "Weekly email summary"
    assert removed.coordinator == "Bea"
    backlog.save(path)
    assert path.read_text() == BACKLOG


def test_in_progress_items_can_be_removed(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(WITH_DONE)
    backlog = Backlog.load(path)
    assert backlog.remove(1).feature == "To-do list app"
    assert [i.number for i in backlog.items] == [2, 3]


def test_missing_items_cannot_be_removed(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(WITH_DONE)
    backlog = Backlog.load(path)
    with pytest.raises(ValueError, match="no backlog item"):
        backlog.remove(99)
    assert backlog.render() == WITH_DONE


def test_new_backlog_items_start_waiting_with_the_next_number(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    item = backlog.add("  Weekly email summary  ")
    assert (item.number, item.feature, item.status) == (3, "Weekly email summary", BacklogStatus.WAITING)
    backlog.save(path)
    assert "| 3 | Weekly email summary | Waiting | |" in path.read_text()


def test_reserved_numbers_are_never_handed_out_again(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(BACKLOG)
    backlog = Backlog.load(path)
    # Item #5's row is gone, but a project still points at it.
    assert backlog.add("Next idea", reserved={5, 1}).number == 6
    assert backlog.add("And another", reserved={2}).number == 7


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


def test_task_list_loads_what_each_task_comes_after(tmp_path):
    path = tmp_path / "tasks.md"
    path.write_text(TASKS)
    assert [t.after for t in TaskList.load(path).tasks] == [[], [1], [1], [], [2, 3]]


def test_old_task_list_without_after_loads_as_none_and_gains_the_column(tmp_path):
    path = tmp_path / "tasks.md"
    path.write_text(OLD_TASKS)
    tasks = TaskList.load(path)
    assert [(t.title, t.after) for t in tasks.tasks] == [("Set up the project", []), ("Dark mode", [])]
    assert "| # | Task | After | Status | Agent | PR |" in tasks.render()
    assert "| 2 | Dark mode | | Not started | | |" in tasks.render()


def test_status_names_are_case_insensitive():
    assert TaskStatus.parse("pending REVIEW") is TaskStatus.PENDING_REVIEW
    with pytest.raises(ValueError):
        TaskStatus.parse("Almost done")
