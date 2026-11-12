import json

from fastapi.testclient import TestClient

from jef.chats import Agent, Role, save_roster
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus
from jef.usage import (
    Usage,
    agent_usage,
    budget_left,
    budget_usd,
    format_cost,
    parse_budget,
)
from jef.usage import format_tokens, project_total, project_usage, usage_from_event
from jef.web.app import create_app
from jef.workspace import Workspace

# Shaped like a real result event from a coordinator's chat log.
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 7,
    "result": "Done.",
    "total_cost_usd": 0.2268,
    "usage": {
        "input_tokens": 12,
        "output_tokens": 3400,
        "cache_creation_input_tokens": 21000,
        "cache_read_input_tokens": 150000,
    },
    "modelUsage": {"claude-opus-5-5": {"inputTokens": 12, "outputTokens": 3400, "costUSD": 0.2268}},
}


def write_log(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events))


def test_usage_from_a_result_event():
    used = usage_from_event(RESULT)
    assert used == Usage(12, 3400, 150000, 21000, 0.2268, 1)
    assert used.total_tokens == 174412


def test_missing_or_odd_values_count_as_zero():
    assert usage_from_event({"type": "result"}) == Usage(turns=1)
    assert usage_from_event({"type": "result", "usage": "nope", "total_cost_usd": None}) == Usage(turns=1)


def test_agent_usage_adds_up_finished_turns_only(tmp_path):
    log = tmp_path / "ada.jsonl"
    write_log(
        log,
        {"type": "jef_sent", "from": "you", "text": "hi"},
        {"type": "assistant", "message": {"content": [], "usage": {"output_tokens": 999}}},
        RESULT,
        "not json {",
        "[1, 2]",
        {**RESULT, "total_cost_usd": 0.1, "usage": {"input_tokens": 8, "output_tokens": 100}},
    )
    used = agent_usage(log)
    assert used.turns == 2
    assert used.input_tokens == 20 and used.output_tokens == 3500
    assert abs(used.cost_usd - 0.3268) < 1e-9
    assert agent_usage(tmp_path / "missing.jsonl") == Usage()


def test_project_usage_by_agent_and_total(tmp_path):
    ws = Workspace(tmp_path)
    chats = ws.project_dir("demo") / "chats"
    write_log(chats / "ada.jsonl", RESULT)
    write_log(chats / "impl-1.jsonl", RESULT, RESULT)
    (chats / "ada.err.log").write_text("noise")
    assert set(project_usage(ws, "demo")) == {"ada", "impl-1"}
    assert project_usage(ws, "demo")["impl-1"].turns == 2
    total = project_total(ws, "demo")
    assert total.turns == 3 and abs(total.cost_usd - 3 * 0.2268) < 1e-9
    assert project_total(ws, "nothing-here") == Usage()


def test_budget_is_read_from_goal_md(tmp_path):
    ws = Workspace(tmp_path)
    folder = ws.project_dir("demo")
    folder.mkdir(parents=True)
    goal = folder / "goal.md"
    goal.write_text("# Goal: Demo\n\n- Repo: o/r\n")
    assert budget_usd(ws, "demo") is None and budget_left(ws, "demo") is None
    goal.write_text("# Goal: Demo\n\n- Repo: o/r\n- Budget: 1.00 USD\n")
    write_log(folder / "chats" / "ada.jsonl", RESULT, RESULT)
    assert budget_usd(ws, "demo") == 1.0
    assert abs(budget_left(ws, "demo") - (1 - 2 * 0.2268)) < 1e-9


def test_parse_budget():
    assert parse_budget("20.00 USD") == 20.0
    assert parse_budget("$5") == 5.0
    for nothing in ("", "none", "0", "-4", "lots", "nan", "inf"):
        assert parse_budget(nothing) is None


def test_formatting():
    assert format_tokens(950) == "950"
    assert format_tokens(12_345) == "12.3k"
    assert format_tokens(999_960) == "1.00M"
    assert format_tokens(4_560_000) == "4.56M"
    assert format_cost(0.2268) == "$0.23"
    assert format_cost(1234.5) == "$1,234.50"


def test_pages_show_usage(tmp_path):
    ws = Workspace(tmp_path)
    folder = ws.project_dir("demo")
    folder.mkdir(parents=True)
    (folder / "goal.md").write_text("# Goal: Demo\n\n- Repo: o/r\n- Coordinator: Ada\n")
    (folder / "tasks.md").write_text("# Tasks: Demo\n")
    save_roster(ws, "demo", [Agent("ada", Role.COORDINATOR, "s1", str(tmp_path)), Agent("impl-1", Role.IMPLEMENTER, "s2", str(tmp_path))])
    write_log(folder / "chats" / "ada.jsonl", RESULT, RESULT)
    app = create_app(ws, github_check=lambda: GitHubStatus(True, "o"), claude_status=ClaudeCheck(True, path="/bin/claude"))
    client = TestClient(app)
    page = client.get("/projects/demo").text
    assert "Est. cost" in page
    assert "348.8k tokens · $0.45 estimated · 2 turns" in page
    assert ">$0.00<" in page  # impl-1 hasn't finished a turn yet
    agent = client.get("/projects/demo/agents/ada").text
    assert "348.8k tokens · $0.45 estimated · 2 turns" in agent
    assert "$0.45" in client.get("/").text
