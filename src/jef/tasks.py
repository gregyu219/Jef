"""A project's task list, kept by its coordinator.

Stored in ``projects/<project>/tasks.md`` as a table:

    # Tasks: To-do list app

    Plan: research.md

    | # | Task | After | Status | Agent | PR |
    | --- | --- | --- | --- | --- | --- |
    | 1 | Set up the project | | Merged | impl-1 | #1 |
    | 2 | Add the to-do list | 1 | Not started | | |

"After" lists the tasks that must merge before a task can start. Older
lists without the column load as having none.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from jef import mdtable

HEADERS = ["#", "Task", "After", "Status", "Agent", "PR"]


class TaskStatus(str, Enum):
    """The five statuses a task moves through, in order."""

    NOT_STARTED = "Not started"
    RESEARCH = "Research"
    IMPLEMENTATION = "Implementation"
    PENDING_REVIEW = "Pending review"
    MERGED = "Merged"

    @classmethod
    def parse(cls, value: str) -> "TaskStatus":
        for status in cls:
            if status.value.lower() == value.strip().lower():
                return status
        raise ValueError(f"Unknown task status: {value!r}")


@dataclass
class Task:
    number: int
    title: str
    status: TaskStatus = TaskStatus.NOT_STARTED
    agent: str = ""
    pr: str = ""
    after: list[int] = field(default_factory=list)  # tasks that must merge before this one starts


def parse_after(value: str) -> list[int]:
    """[1, 2] from "1, 2"; anything that isn't a number is ignored."""
    return [int(part) for part in value.replace(",", " ").split() if part.isdigit()]


def format_after(numbers: list[int]) -> str:
    return ", ".join(map(str, numbers))


class TaskList:
    def __init__(self, tasks: list[Task] | None = None, doc: mdtable.TableDoc | None = None, title: str = ""):
        self.tasks = tasks or []
        self._doc = doc or mdtable.TableDoc(before=[f"# Tasks: {title}".rstrip(": "), ""])

    @property
    def title(self) -> str:
        """The project name from the '# Tasks: <name>' heading, if there is one."""
        for line in self._doc.before:
            if line.startswith("# "):
                heading = line[2:].strip()
                return heading.split(":", 1)[1].strip() if ":" in heading else heading
        return ""

    @classmethod
    def load(cls, path: Path) -> "TaskList":
        if not path.exists():
            return cls()
        doc = mdtable.parse(path.read_text(encoding="utf-8"))
        if not doc.headers:
            return cls(doc=doc)
        col = {name.lower(): i for i, name in enumerate(doc.headers)}

        def cell(row: list[str], name: str) -> str:
            return row[col[name]] if name in col else ""

        tasks = []
        for row in doc.rows:
            number = cell(row, "#")
            status = cell(row, "status")
            tasks.append(
                Task(
                    number=int(number) if number.isdigit() else len(tasks) + 1,
                    title=cell(row, "task"),
                    status=TaskStatus.parse(status) if status else TaskStatus.NOT_STARTED,
                    agent=cell(row, "agent"),
                    pr=cell(row, "pr"),
                    after=parse_after(cell(row, "after")),
                )
            )
        return cls(tasks, doc)

    def counts(self) -> dict[TaskStatus, int]:
        """How many tasks are in each status, in status order."""
        c = Counter(t.status for t in self.tasks)
        return {s: c.get(s, 0) for s in TaskStatus}

    def render(self) -> str:
        self._doc.headers = list(HEADERS)
        self._doc.rows = [
            [str(t.number), t.title, format_after(t.after), t.status.value, t.agent, t.pr] for t in self.tasks
        ]
        return mdtable.render(self._doc)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
