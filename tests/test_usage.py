import json
import os
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from jef.chats import Agent, Role, save_roster
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus
from jef.usage import (
    Usage,
    agent_usage,
    budget_left,
    budget_usd,
    SpendingLimit,
    format_cost,
    latest_plan_usage,
    limit_left,
    parse_budget,
    parse_limit,
    period_start,
    plan_windows,
    set_spending_limit,
    spending_limit,
    spent_since,
)
from jef.usage import format_tokens, project_total, project_usage, usage_from_event
from jef.web.app import create_app, format_reset
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


FUTURE = 4_000_000_000  # year 2096
LATER = FUTURE + 300_000


def rate_event(five=0.73, week=0.07, five_reset=FUTURE, week_reset=LATER, status="allowed"):
    """Shaped like a real rate_limit_event from an agent's chat log."""
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status, "resetsAt": five_reset, "rateLimitType": "five_hour",
            "overageStatus": "rejected", "isUsingOverage": False,
            "unifiedWindows": {
                "five_hour": {"utilization": five, "resetsAt": five_reset, "status": status},
                "seven_day": {"utilization": week, "resetsAt": week_reset, "status": "allowed"},
            },
        },
        "session_id": "s1",
    }


def test_plan_windows_from_unified_windows():
    five, week = plan_windows(rate_event())
    assert (five.name, five.label, five.percent, five.status) == ("five_hour", "Current session (5 hours)", 73, "allowed")
    assert five.resets_at == datetime.fromtimestamp(FUTURE, tz=timezone.utc)
    assert (week.label, week.percent) == ("This week", 7)


def test_plan_windows_from_the_older_top_level_format():
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "rateLimitType": "seven_day_opus", "utilization": 0.9, "resetsAt": FUTURE}}
    (window,) = plan_windows(event)
    assert (window.label, window.percent, window.status) == ("Seven day opus", 90, "allowed_warning")


def test_plan_windows_skip_odd_values():
    assert plan_windows({"type": "rate_limit_event"}) == []
    assert plan_windows({"rate_limit_info": {"rateLimitType": "five_hour"}}) == []
    event = {"rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": "lots"}, "seven_day": {"utilization": 0.5, "resetsAt": "soon"}, "x": 3}}}
    (week,) = plan_windows(event)
    assert week.name == "seven_day" and week.resets_at is None


def test_reset_since():
    window = plan_windows(rate_event(five_reset=1_000))[0]
    assert window.reset_since(datetime.now(timezone.utc))
    assert not plan_windows(rate_event())[0].reset_since(datetime.now(timezone.utc))


def test_latest_plan_usage_takes_the_last_event_in_the_newest_log(tmp_path):
    ws = Workspace(tmp_path)
    assert latest_plan_usage(ws) is None
    old = ws.project_dir("a") / "chats" / "ada.jsonl"
    new = ws.project_dir("b") / "chats" / "impl-1.jsonl"
    write_log(old, rate_event(five=0.99))
    write_log(new, rate_event(five=0.10), "not json {", rate_event(five=0.20), RESULT, "rate_limit_event broken")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    plan = latest_plan_usage(ws)
    assert plan.windows[0].percent == 20
    assert plan.seen_at == datetime.fromtimestamp(2_000_000, tz=timezone.utc)
    # A newer log with no plan usage in it is passed over.
    empty = ws.project_dir("b") / "chats" / "impl-2.jsonl"
    write_log(empty, RESULT)
    assert latest_plan_usage(ws).windows[0].percent == 20


def test_latest_plan_usage_includes_the_home_chat(tmp_path):
    ws = Workspace(tmp_path)
    project = ws.project_dir("a") / "chats" / "ada.jsonl"
    home = tmp_path / "jef" / "projects" / "home" / "chats" / "jef.jsonl"
    write_log(project, rate_event(five=0.30))
    write_log(home, rate_event(five=0.40))
    os.utime(project, (1_000_000, 1_000_000))
    os.utime(home, (2_000_000, 2_000_000))
    assert latest_plan_usage(ws).windows[0].percent == 40


def test_format_reset():
    now = datetime(2026, 11, 11, 9, 0).astimezone()
    assert format_reset(now.replace(hour=15), now) == "3:00 pm"
    assert format_reset(now.replace(hour=0, minute=5), now) == "12:05 am"
    assert format_reset(datetime(2026, 11, 14, 9, 30).astimezone(), now) == "Sat 14 Nov, 9:30 am"


def test_home_page_shows_plan_usage_and_total_cost(tmp_path):
    ws = Workspace(tmp_path)
    for slug in ("demo", "other"):
        (ws.project_dir(slug)).mkdir(parents=True)
        (ws.project_dir(slug) / "tasks.md").write_text("# Tasks: Demo\n")
        write_log(ws.project_dir(slug) / "chats" / "ada.jsonl", RESULT)
    write_log(ws.project_dir("demo") / "chats" / "ada.jsonl", RESULT, rate_event(five=0.75, week=0.07))
    client = TestClient(create_app(ws, github_check=lambda: GitHubStatus(True, "o")))
    page = client.get("/").text
    assert "Current session (5 hours)" in page and "75%" in page
    assert "This week" in page and "7%" in page
    assert "resets " in page and "As of just now" in page
    assert "limit reached" not in page
    assert "$0.45" in page  # both projects added up


def test_home_page_shows_a_reached_limit_and_reset_windows(tmp_path):
    ws = Workspace(tmp_path)
    write_log(ws.project_dir("demo") / "chats" / "ada.jsonl", rate_event(five=1.0, status="rejected", week_reset=1_000))
    page = TestClient(create_app(ws, github_check=lambda: GitHubStatus(True, "o"))).get("/").text
    assert "Current session (5 hours): limit reached" in page
    assert "Reset since" in page


def test_home_page_without_plan_usage(tmp_path):
    page = TestClient(create_app(Workspace(tmp_path), github_check=lambda: GitHubStatus(True, "o"))).get("/").text
    assert "No plan usage seen yet" in page
    assert "$0.00" in page


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


def sent(at):
    return {"type": "jef_sent", "from": "you", "text": "hi", "at": at}


def test_dated_turns_count_toward_the_day_and_month_they_started(tmp_path):
    now = datetime(2026, 11, 11, 15, 0).astimezone()
    log = tmp_path / "ada.jsonl"
    write_log(
        log,
        RESULT,  # from before Jef dated its messages: not in any period
        sent(datetime(2026, 10, 31, 23, 0).astimezone().isoformat()),
        {**RESULT, "total_cost_usd": 1.0},  # last month
        sent(datetime(2026, 11, 2, 9, 0).astimezone().isoformat()),
        {**RESULT, "total_cost_usd": 2.0},  # this month
        sent("2026-11-11T08:00:00"),  # no zone: local time
        {**RESULT, "total_cost_usd": 4.0},  # today
        sent("nonsense"),
        {**RESULT, "total_cost_usd": 8.0},  # undated
    )
    assert period_start("day", now) == datetime(2026, 11, 11).astimezone()
    assert period_start("month", now) == datetime(2026, 11, 1).astimezone()
    assert agent_usage(log, period_start("day", now)).cost_usd == 4.0
    assert agent_usage(log, period_start("month", now)).cost_usd == 6.0
    assert abs(agent_usage(log).cost_usd - 15.2268) < 1e-9  # no date given: everything

    ws = Workspace(tmp_path / "jef")
    write_log(ws.project_dir("one") / "chats" / "ada.jsonl", sent(now.isoformat()), {**RESULT, "total_cost_usd": 1.5})
    write_log(ws.project_dir("two") / "chats" / "bo.jsonl", sent(now.isoformat()), {**RESULT, "total_cost_usd": 2.5})
    assert spent_since(ws, period_start("day", now)) == 4.0


def test_spending_limit_in_settings_md(tmp_path):
    assert parse_limit("50.00 USD per day") == SpendingLimit(50.0, "day")
    assert parse_limit("$200 per month") == SpendingLimit(200.0, "month")
    for nothing in ("", "none", "50 USD", "50 per week", "0 USD per day", "lots per day"):
        assert parse_limit(nothing) is None

    ws = Workspace(tmp_path)
    assert spending_limit(ws) is None and limit_left(ws) is None
    set_spending_limit(ws, SpendingLimit(5, "day"))
    assert ws.settings_file.read_text() == "# Jef settings\n\n- Spending limit: 5.00 USD per day\n"
    ws.settings_file.write_text(ws.settings_file.read_text() + "- Something else: kept\n")
    set_spending_limit(ws, SpendingLimit(30, "month"))
    set_spending_limit(ws, SpendingLimit(3, "day"))
    text = ws.settings_file.read_text()
    assert text.count("Spending limit") == 1 and "Something else: kept" in text
    now = datetime.now().astimezone()
    write_log(ws.project_dir("demo") / "chats" / "ada.jsonl", sent(now.isoformat()), {**RESULT, "total_cost_usd": 1.25})
    assert limit_left(ws, now) == 1.75
    set_spending_limit(ws, None)
    assert spending_limit(ws) is None and "Spending limit: none" in ws.settings_file.read_text()
