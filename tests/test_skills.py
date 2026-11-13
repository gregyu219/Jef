from jef import skills


def test_the_coordinator_plans_tasks_to_run_side_by_side():
    text = skills.load("coordinator")
    assert "Plan in waves" in text
    assert "Split by files and modules" in text
    assert "[after N]" in text
    # It no longer falls back to building tasks one by one when unsure.
    assert "run them one after the other" not in text


def test_the_coordinator_leaves_starting_ready_tasks_to_jef():
    text = skills.load("coordinator")
    assert "Jef starts the tasks for you" in text
    assert "Jef starts ready tasks: yes" in text  # how it tells, from jef overview
    assert "start every task that's now unblocked" not in text
    assert "one `jef task start N` each" not in text


def test_a_missing_skill_loads_as_empty(tmp_path):
    assert skills.load("nobody", skills_dir=tmp_path) == ""
