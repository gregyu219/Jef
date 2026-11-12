"""How many tokens each agent has used, and what Claude Code estimates it cost.

Every finished turn ends with a ``result`` event in the agent's chat log,
``projects/<project>/chats/<name>.jsonl``. That event carries the turn's token
counts and ``total_cost_usd``, Claude Code's own estimate at list price. Jef
adds those up straight from the logs, so there's no separate usage file to
drift out of step. A turn that was stopped before it finished writes no
``result`` event, so its usage isn't counted.

A project can have a dollar budget, a ``- Budget: 20.00 USD`` line in its
goal.md. ``budget_left`` is what Jef checks before starting any turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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


def agent_usage(chat_file: Path) -> Usage:
    """Everything one agent has used, added up from its chat log."""
    total = Usage()
    if not chat_file.exists():
        return total
    for raw in chat_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
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


def format_tokens(count: int) -> str:
    """Short and readable: 950, 12.3k, 4.56M."""
    if count < 1000:
        return str(count)
    if round(count / 1000, 1) < 1000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def format_cost(usd: float) -> str:
    return f"${usd:,.2f}"
