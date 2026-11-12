"""How many tokens each agent has used, and what Claude Code estimates it cost.

Every finished turn ends with a ``result`` event in the agent's chat log,
``projects/<project>/chats/<name>.jsonl``. That event carries the turn's token
counts and ``total_cost_usd``, Claude Code's own estimate at list price. Jef
adds those up straight from the logs, so there's no separate usage file to
drift out of step. A turn that was stopped before it finished writes no
``result`` event, so its usage isn't counted.

A project can have a dollar budget, a ``- Budget: 20.00 USD`` line in its
goal.md. ``budget_left`` is what Jef checks before starting any turn.

Claude Code also writes ``rate_limit_event`` lines into the same logs: how
much of your plan's 5-hour and weekly limits you've used. ``latest_plan_usage``
reads the newest one, so the home page can show it without any extra call.

There can also be an overall spending limit across all projects, per day or
per month: a ``- Spending limit: 50.00 USD per day`` line in Jef's
settings.md. ``limit_left`` is checked before every turn too. To know which
day a turn belongs to, Jef dates each message it sends (``jef_sent``'s
``at``), and a turn counts on the day it started.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jef import jefchat
from jef.projects import goal_fields
from jef.workspace import Workspace


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0  # finished turns (one ``claude -p`` run each)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cost_usd + other.cost_usd,
            self.turns + other.turns,
        )


def _int(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def usage_from_event(event: dict) -> Usage:
    """The usage in one ``result`` event. Missing or odd values count as 0."""
    tokens = event.get("usage")
    if not isinstance(tokens, dict):
        tokens = {}
    return Usage(
        input_tokens=_int(tokens.get("input_tokens")),
        output_tokens=_int(tokens.get("output_tokens")),
        cache_read_tokens=_int(tokens.get("cache_read_input_tokens")),
        cache_write_tokens=_int(tokens.get("cache_creation_input_tokens")),
        cost_usd=_float(event.get("total_cost_usd")),
        turns=1,
    )


def _sent_at(event: dict) -> datetime | None:
    """When Jef sent a message, from a ``jef_sent`` event's ``at``."""
    try:
        when = datetime.fromisoformat(str(event.get("at") or ""))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()  # no zone: local time


def agent_usage(chat_file: Path, since: datetime | None = None) -> Usage:
    """Everything one agent has used, added up from its chat log.

    With ``since``, only turns that started then or later: a turn counts at
    the time Jef sent its message. Turns from before Jef dated its messages
    have no date, so they're left out."""
    total = Usage()
    if not chat_file.exists():
        return total
    started: datetime | None = None
    for raw in chat_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "jef_sent":
            started = _sent_at(event)
        elif event.get("type") == "result":
            if since is None or (started is not None and started >= since):
                total += usage_from_event(event)
    return total


def project_usage(ws: Workspace, slug: str) -> dict[str, Usage]:
    """Usage for every agent in a project that has a chat log, by agent name."""
    chats = ws.project_dir(slug) / "chats"
    if not chats.is_dir():
        return {}
    return {path.stem: agent_usage(path) for path in sorted(chats.glob("*.jsonl"))}


def project_total(ws: Workspace, slug: str) -> Usage:
    total = Usage()
    for used in project_usage(ws, slug).values():
        total += used
    return total


def parse_budget(value: str) -> float | None:
    """The dollar amount in a goal.md ``Budget`` value like "20.00 USD" or
    "$20", or None for "none", empty or nonsense."""
    text = value.strip().lower().removeprefix("$").removesuffix("usd").strip()
    try:
        amount = float(text)
    except ValueError:
        return None
    return amount if amount > 0 and amount != float("inf") else None


def budget_usd(ws: Workspace, slug: str) -> float | None:
    """The project's dollar budget from goal.md, or None if it has none."""
    return parse_budget(goal_fields(ws, slug).get("budget", ""))


def budget_left(ws: Workspace, slug: str) -> float | None:
    """Dollars left in the project's budget (0 or less once it's used up),
    or None if the project has no budget."""
    budget = budget_usd(ws, slug)
    if budget is None:
        return None
    return budget - project_total(ws, slug).cost_usd


WINDOW_LABELS = {
    "five_hour": "Current session (5 hours)",
    "seven_day": "This week",
}


@dataclass(frozen=True)
class PlanWindow:
    """One plan limit, like the 5-hour session or the week."""

    name: str
    utilization: float  # 0 to 1 (can go past 1 when over the limit)
    resets_at: datetime | None
    status: str = ""

    @property
    def label(self) -> str:
        return WINDOW_LABELS.get(self.name) or self.name.replace("_", " ").strip().capitalize()

    @property
    def percent(self) -> int:
        return round(self.utilization * 100)

    def reset_since(self, now: datetime) -> bool:
        """True once the window has reset, so the saved percentage is stale."""
        return self.resets_at is not None and self.resets_at <= now


@dataclass(frozen=True)
class PlanUsage:
    windows: list[PlanWindow] = field(default_factory=list)
    seen_at: datetime | None = None  # when the log it came from was written


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _time(value) -> datetime | None:
    seconds = _number(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _window(name, info) -> PlanWindow | None:
    if not isinstance(name, str) or not name or not isinstance(info, dict):
        return None
    utilization = _number(info.get("utilization"))
    if utilization is None:
        return None
    status = info.get("status")
    return PlanWindow(name, max(utilization, 0.0), _time(info.get("resetsAt")), status if isinstance(status, str) else "")


def plan_windows(event: dict) -> list[PlanWindow]:
    """The plan limits in one ``rate_limit_event``. Odd values are skipped."""
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return []
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        windows = [_window(name, window) for name, window in unified.items()]
    else:
        windows = [_window(info.get("rateLimitType"), info)]
    return [w for w in windows if w is not None]


def _last_plan_windows(path: Path) -> list[PlanWindow]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for raw in reversed(lines):
        if "rate_limit_event" not in raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "rate_limit_event":
            windows = plan_windows(event)
            if windows:
                return windows
    return []


def latest_plan_usage(ws: Workspace) -> PlanUsage | None:
    """Your plan usage from the newest chat log that has any, across every
    project and the home page's Jef chat. None if no log has any yet."""
    folders = [ws.project_dir(slug) / "chats" for slug in ws.project_slugs()]
    folders.append(jefchat.jef_workspace(ws).project_dir(jefchat.HOME) / "chats")
    logs = []
    for folder in folders:
        for path in folder.glob("*.jsonl") if folder.is_dir() else []:
            try:
                logs.append((path.stat().st_mtime, str(path), path))
            except OSError:
                continue
    for mtime, _, path in sorted(logs, reverse=True):
        windows = _last_plan_windows(path)
        if windows:
            return PlanUsage(windows, datetime.fromtimestamp(mtime, tz=timezone.utc))
    return None


PERIODS = ("day", "month")


@dataclass(frozen=True)
class SpendingLimit:
    """At most ``amount_usd`` across all projects each day or each month."""

    amount_usd: float
    period: str  # "day" or "month"

    def __str__(self) -> str:
        return f"{self.amount_usd:.2f} USD per {self.period}"

    @property
    def when(self) -> str:
        return "today" if self.period == "day" else "this month"


def parse_limit(value: str) -> SpendingLimit | None:
    """A settings.md ``Spending limit`` value like "50.00 USD per day", or
    None for "none", empty or nonsense."""
    amount, sep, period = value.strip().lower().partition(" per ")
    if not sep or period.strip() not in PERIODS:
        return None
    usd = parse_budget(amount)
    return SpendingLimit(usd, period.strip()) if usd is not None else None


def _settings_fields(ws: Workspace) -> dict[str, str]:
    fields: dict[str, str] = {}
    if not ws.settings_file.exists():
        return fields
    for line in ws.settings_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            fields[key.strip().lower()] = value.strip()
    return fields


def spending_limit(ws: Workspace) -> SpendingLimit | None:
    """The overall limit from settings.md, or None if there's none."""
    return parse_limit(_settings_fields(ws).get("spending limit", ""))


def set_spending_limit(ws: Workspace, limit: SpendingLimit | None) -> None:
    """Write the overall limit (or "none") into settings.md, keeping any
    other lines there."""
    new = f"- Spending limit: {limit if limit else 'none'}"
    path = ws.settings_file
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["# Jef settings", ""]
    for i, line in enumerate(lines):
        if line.startswith("- ") and line[2:].split(":", 1)[0].strip().lower() == "spending limit":
            lines[i] = new
            break
    else:
        lines.append(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def period_start(period: str, now: datetime | None = None) -> datetime:
    """Local midnight at the start of today, or of the 1st of this month."""
    local = (now or datetime.now()).astimezone().replace(tzinfo=None)
    start = local.replace(day=1 if period == "month" else local.day, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone()  # its own UTC offset, in case daylight saving changed since


def spent_since(ws: Workspace, since: datetime) -> float:
    """Estimated dollars spent across all projects on turns started since then."""
    return sum(
        agent_usage(path, since).cost_usd
        for slug in ws.project_slugs()
        for path in sorted((ws.project_dir(slug) / "chats").glob("*.jsonl"))
    )


def limit_spent(ws: Workspace, limit: SpendingLimit, now: datetime | None = None) -> float:
    return spent_since(ws, period_start(limit.period, now))


def limit_left(ws: Workspace, now: datetime | None = None) -> float | None:
    """Dollars left under the overall limit this day or month (0 or less once
    it's used up), or None if there's no limit."""
    limit = spending_limit(ws)
    if limit is None:
        return None
    return limit.amount_usd - limit_spent(ws, limit, now)


def format_tokens(count: int) -> str:
    """Short and readable: 950, 12.3k, 4.56M."""
    if count < 1000:
        return str(count)
    if round(count / 1000, 1) < 1000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def format_cost(usd: float) -> str:
    return f"${usd:,.2f}"
