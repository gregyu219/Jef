import json

import pytest

from jef.chats import ALWAYS_DENIED, Chats, ChatError, Role, Status, load_roster
from jef.workspace import Workspace


class FakeProc:
    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self.stdin_text = kwargs["stdin"].read().decode()
        self.done = False
        self.terminated = False

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.terminated = True
        self.done = True


@pytest.fixture
def setup(tmp_path):
    ws = Workspace(tmp_path / "jef")
    repo = tmp_path / "repo"
    repo.mkdir()
    procs = []
    ids = iter(f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 10))

    def popen(cmd, **kwargs):
        proc = FakeProc(cmd, **kwargs)
        procs.append(proc)
        return proc

    chats = Chats(ws, "/bin/claude", popen=popen, new_session_id=lambda: next(ids))
    return ws, repo, chats, procs


def test_spawn_starts_a_named_session_and_records_it(setup):
    ws, repo, chats, procs = setup
    agent = chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build task 1", repo, instructions="Follow the skill.")
    cmd = procs[0].cmd
    assert cmd[:2] == ["/bin/claude", "-p"]
    assert cmd[cmd.index("--session-id") + 1] == agent.session
    assert "jef · todo · impl-1" in cmd
    assert procs[0].stdin_text == "Build task 1"  # the message goes in on stdin, not the command line
    assert procs[0].kwargs["cwd"] == str(repo.resolve())
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Follow the skill."
    for rule in ALWAYS_DENIED:
        assert rule in cmd
    assert "--bare" not in cmd
    assert procs[0].kwargs["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    roster = load_roster(ws, "todo")
    assert [(a.name, a.role, a.status) for a in roster] == [("impl-1", Role.IMPLEMENTER, Status.WORKING)]


def test_send_resumes_the_same_conversation_after_the_turn_ends(setup):
    ws, repo, chats, procs = setup
    agent = chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build task 1", repo)
    with pytest.raises(ChatError, match="still working"):
        chats.send("todo", "impl-1", "Any news?")
    procs[0].done = True
    assert chats.refresh("todo")[0].status is Status.IDLE
    chats.send("todo", "impl-1", "Please also add a test", sender="coordinator")
    cmd = procs[1].cmd
    assert cmd[cmd.index("--resume") + 1] == agent.session
    assert "--session-id" not in cmd
    sent = [l for l in chats.transcript("todo", "impl-1") if l.kind == "sent"]
    assert [l.text for l in sent] == ["you: Build task 1", "coordinator: Please also add a test"]


def test_turns_are_capped_at_what_is_left_of_the_budget(setup):
    ws, repo, chats, procs = setup
    chats.spawn("todo", "free", Role.IMPLEMENTER, "No budget", repo)
    assert "--max-budget-usd" not in procs[0].cmd
    left = {"todo": 2.5}
    chats.budget_left = lambda project: left[project]
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build it", repo)
    cmd = procs[1].cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.50"
    procs[1].done = True
    left["todo"] = 0.004  # a sliver left still runs, capped at a cent
    chats.send("todo", "impl-1", "more")
    assert procs[2].cmd[procs[2].cmd.index("--max-budget-usd") + 1] == "0.01"


def test_each_turn_runs_on_the_model_chosen_for_its_level(setup):
    ws, repo, chats, procs = setup
    chats.spawn("todo", "free", Role.IMPLEMENTER, "No model chosen", repo)
    assert "--model" not in procs[0].cmd
    chosen = {Role.IMPLEMENTER: "opus"}
    chats.model_for = lambda project, role: chosen.get(role, "")
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build it", repo)
    cmd = procs[1].cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    procs[1].done = True
    chosen[Role.IMPLEMENTER] = "sonnet"  # changed between turns: the next turn uses it
    chats.send("todo", "impl-1", "more")
    assert procs[2].cmd[procs[2].cmd.index("--model") + 1] == "sonnet"


def test_no_turn_starts_once_the_budget_is_used_up(setup):
    ws, repo, chats, procs = setup
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build it", repo)
    procs[0].done = True
    chats.budget_left = lambda project: -0.2
    with pytest.raises(ChatError, match="budget is used up"):
        chats.send("todo", "impl-1", "more")
    with pytest.raises(ChatError, match="budget is used up"):
        chats.spawn("todo", "impl-2", Role.IMPLEMENTER, "Build it", repo)
    assert len(procs) == 1
    assert [(a.name, a.status) for a in load_roster(ws, "todo")] == [("impl-1", Status.IDLE)]


def test_replies_keep_the_result_subtype(setup):
    ws, repo, chats, procs = setup
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build it", repo)
    with chats.chat_file("todo", "impl-1").open("a") as f:
        f.write(json.dumps({"type": "result", "is_error": True, "subtype": "error_max_budget_usd"}) + "\n")
    assert chats.replies("todo", "impl-1")[0].subtype == "error_max_budget_usd"
    assert chats.transcript("todo", "impl-1")[-1].text.startswith("Stopped partway")


def test_stopping_a_coordinator_stops_its_workers(setup):
    ws, repo, chats, procs = setup
    chats.spawn("todo", "ada", Role.COORDINATOR, "Plan it", repo)
    chats.spawn("todo", "research", Role.RESEARCH, "Look into it", repo, parent="ada")
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build it", repo, parent="research")
    chats.spawn("todo", "other", Role.IMPLEMENTER, "Unrelated", repo)
    assert sorted(chats.stop("todo", "ada")) == ["ada", "impl-1", "research"]
    assert [p.terminated for p in procs] == [True, True, True, False]
    statuses = {a.name: a.status for a in load_roster(ws, "todo")}
    assert statuses == {"ada": Status.STOPPED, "research": Status.STOPPED, "impl-1": Status.STOPPED, "other": Status.WORKING}
    with pytest.raises(ChatError, match="stopped"):
        chats.send("todo", "ada", "hello")


def test_a_turn_cut_off_by_a_restart_counts_as_finished(setup):
    ws, repo, chats, procs = setup
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build", repo)
    fresh = Chats(ws, "/bin/claude", popen=chats.popen)  # Jef restarted: no live processes
    assert fresh.refresh("todo")[0].status is Status.IDLE


def test_bad_names_duplicates_and_unknown_parents(setup):
    ws, repo, chats, procs = setup
    with pytest.raises(ChatError, match="lowercase"):
        chats.spawn("todo", "../escape", Role.IMPLEMENTER, "x", repo)
    with pytest.raises(ChatError, match="doesn't exist"):
        chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "x", repo / "nope")
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "x", repo)
    with pytest.raises(ChatError, match="already"):
        chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "x", repo)
    with pytest.raises(ChatError, match="no agent named"):
        chats.spawn("todo", "impl-2", Role.IMPLEMENTER, "x", repo, parent="ghost")


def test_transcript_reads_claude_code_stream_events(setup):
    ws, repo, chats, procs = setup
    chats.spawn("todo", "impl-1", Role.IMPLEMENTER, "Build", repo)
    with chats.chat_file("todo", "impl-1").open("a") as f:
        for event in [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Running the tests."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
            ]}},
            {"type": "result", "is_error": False, "result": "done"},
        ]:
            f.write(json.dumps(event) + "\n")
        f.write("not json\n")
    kinds = [(l.kind, l.text) for l in chats.transcript("todo", "impl-1")]
    assert kinds == [("sent", "you: Build"), ("text", "Running the tests."), ("tool", "Bash pytest -q"), ("result", "Turn finished.")]


def test_pushing_is_refused_even_with_git_options_first():
    from jef.chats import ALWAYS_DENIED

    for rule in ("Bash(git push*)", "Bash(git -C * push*)", "Bash(git -c * push*)"):
        assert rule in ALWAYS_DENIED
