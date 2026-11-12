"""Which Claude model each level of a project runs on.

A project has three levels: its coordinator (heartbeats, chat, PR reviews),
its research agent and its implementers. Each gets a model, chosen with a
"- Coordinator model: haiku" line at the top of the project's goal.md. A level
without a line uses the default below.

The values are Claude Code's ``--model`` aliases, which follow the latest
version of each model. "default" means don't pass ``--model`` at all, so the
agent runs on Claude Code's own default.
"""

from __future__ import annotations

from jef.chats import Role
from jef.projects import goal_fields
from jef.workspace import Workspace

# The goal.md key for each level's model.
LEVELS: dict[Role, str] = {
    Role.COORDINATOR: "Coordinator model",
    Role.RESEARCH: "Research model",
    Role.IMPLEMENTER: "Implementer model",
}

# Sonnet for the coordinator: cheaper than Opus for routine heartbeats, but it
# also reviews PRs, fixes conflicts and chats with the person, so not Haiku
# (a project can still choose Haiku). Opus for research and building.
DEFAULTS: dict[Role, str] = {
    Role.COORDINATOR: "sonnet",
    Role.RESEARCH: "opus",
    Role.IMPLEMENTER: "opus",
}

CLAUDE_DEFAULT = "default"  # leave --model out
CHOICES = ["haiku", "sonnet", "opus", "fable", CLAUDE_DEFAULT]


def model_setting(ws: Workspace, slug: str, role: Role) -> str:
    """The level's model as chosen for this project: one of CHOICES.

    An unknown value in goal.md (a typo, say) falls back to the default."""
    if role not in LEVELS:
        return CLAUDE_DEFAULT
    value = goal_fields(ws, slug).get(LEVELS[role].lower(), "").strip().lower()
    return value if value in CHOICES else DEFAULTS[role]


def model_for(ws: Workspace, slug: str, role: Role) -> str:
    """What to pass as ``--model`` for this agent's turn, or "" for none."""
    setting = model_setting(ws, slug, role)
    return "" if setting == CLAUDE_DEFAULT else setting
