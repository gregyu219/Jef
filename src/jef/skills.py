"""The skills Jef's agents follow: markdown files under ``skills/<role>/SKILL.md``."""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def load(role: str, skills_dir: Path = SKILLS_DIR) -> str:
    """The skill's text without its front matter, or "" if there isn't one."""
    path = skills_dir / role / "SKILL.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip()
