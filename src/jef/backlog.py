"""Jef's backlog: everything you have asked for, started or not.

Stored in ``backlog.md`` as a table:

    # Backlog

    | # | Feature | Status | Coordinator |
    | --- | --- | --- | --- |
    | 1 | To-do list app | In progress | Ada |
    | 2 | Dark mode | Waiting | |
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from jef import mdtable

HEADERS = ["#", "Feature", "Status", "Coordinator"]


class BacklogStatus(str, Enum):
    WAITING = "Waiting"
    IN_PROGRESS = "In progress"
    DONE = "Done"

    @classmethod
    def parse(cls, value: str) -> "BacklogStatus":
        for status in cls:
            if status.value.lower() == value.strip().lower():
                return status
        raise ValueError(f"Unknown backlog status: {value!r}")


@dataclass
class BacklogItem:
    number: int
    feature: str
    status: BacklogStatus = BacklogStatus.WAITING
    coordinator: str = ""


class Backlog:
    def __init__(self, items: list[BacklogItem] | None = None, doc: mdtable.TableDoc | None = None):
        self.items = items or []
        self._doc = doc or mdtable.TableDoc(before=["# Backlog", ""])

    @classmethod
    def load(cls, path: Path) -> "Backlog":
        if not path.exists():
            return cls()
        doc = mdtable.parse(path.read_text(encoding="utf-8"))
        if not doc.headers:
            return cls(doc=doc)
        col = {name.lower(): i for i, name in enumerate(doc.headers)}
        items = []
        for row in doc.rows:
            number = row[col["#"]] if "#" in col else ""
            items.append(
                BacklogItem(
                    number=int(number) if number.isdigit() else len(items) + 1,
                    feature=row[col["feature"]] if "feature" in col else "",
                    status=BacklogStatus.parse(row[col["status"]]) if "status" in col and row[col["status"]] else BacklogStatus.WAITING,
                    coordinator=row[col["coordinator"]] if "coordinator" in col else "",
                )
            )
        return cls(items, doc)

    def add(self, feature: str) -> BacklogItem:
        feature = feature.strip()
        if not feature:
            raise ValueError("A backlog item needs a description.")
        number = max((i.number for i in self.items), default=0) + 1
        item = BacklogItem(number=number, feature=feature)
        self.items.append(item)
        return item

    def remove(self, number: int) -> BacklogItem:
        """Remove a Waiting item. Started or finished items have a project
        pointing at their number, so they can't be removed."""
        item = next((i for i in self.items if i.number == number), None)
        if item is None:
            raise ValueError(f"There is no backlog item #{number}.")
        if item.status != BacklogStatus.WAITING:
            raise ValueError(f"Backlog item #{number} has started, so it can't be deleted.")
        self.items.remove(item)
        return item

    def render(self) -> str:
        self._doc.headers = list(HEADERS)
        self._doc.rows = [
            [str(i.number), i.feature, i.status.value, i.coordinator] for i in self.items
        ]
        return mdtable.render(self._doc)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
