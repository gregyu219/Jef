"""Where Jef keeps its files.

Everything lives under one folder, ``~/.jef`` by default. Set the
``JEF_HOME`` environment variable to use a different folder (handy for
tests and for trying things out).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def default(cls) -> "Workspace":
        home = os.environ.get("JEF_HOME")
        return cls(Path(home).expanduser() if home else Path.home() / ".jef")

    @property
    def backlog_file(self) -> Path:
        return self.root / "backlog.md"

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    def project_dir(self, slug: str) -> Path:
        return self.projects_dir / slug

    def project_slugs(self) -> list[str]:
        """Names of every project folder, sorted."""
        if not self.projects_dir.is_dir():
            return []
        return sorted(p.name for p in self.projects_dir.iterdir() if p.is_dir())
