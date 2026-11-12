from jef import models
from jef.chats import Role
from jef.projects import set_goal_field
from jef.workspace import Workspace


def make_project(tmp_path) -> Workspace:
    ws = Workspace(tmp_path / "jef")
    ws.project_dir("todo").mkdir(parents=True)
    (ws.project_dir("todo") / "goal.md").write_text(
        "# Goal: Todo\n\n- Repo: me/todo\n\n## What you asked for\n\nA todo app\n", encoding="utf-8"
    )
    return ws


def test_each_level_has_a_default_model(tmp_path):
    ws = make_project(tmp_path)
    assert models.model_for(ws, "todo", Role.COORDINATOR) == "haiku"
    assert models.model_for(ws, "todo", Role.RESEARCH) == "opus"
    assert models.model_for(ws, "todo", Role.IMPLEMENTER) == "opus"
    assert models.model_for(ws, "todo", Role.JEF) == ""  # not a project level
    assert models.model_for(ws, "missing", Role.COORDINATOR) == "haiku"


def test_goal_md_chooses_a_level_s_model(tmp_path):
    ws = make_project(tmp_path)
    set_goal_field(ws, "todo", "Coordinator model", "Sonnet")
    set_goal_field(ws, "todo", "Implementer model", "default")
    set_goal_field(ws, "todo", "Research model", "gpt-9")  # unknown: fall back
    assert models.model_for(ws, "todo", Role.COORDINATOR) == "sonnet"
    assert models.model_setting(ws, "todo", Role.IMPLEMENTER) == "default"
    assert models.model_for(ws, "todo", Role.IMPLEMENTER) == ""  # Claude Code's own default
    assert models.model_for(ws, "todo", Role.RESEARCH) == "opus"
