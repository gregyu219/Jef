import subprocess
from pathlib import Path

import pytest

from jef.publish import Publisher, PublishError, find_secrets_in_diff

FAKE_KEY = "sk-ant-api03-" + "z" * 40


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def setup(tmp_path):
    """A real repo with a local "origin", and gh faked out."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "switch", "-q", "-c", "jef/demo/task-1")
    gh_calls = []
    open_prs = {}

    def run(cmd, **kwargs):
        if cmd[0] == "gh":
            gh_calls.append(cmd)
            if cmd[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, open_prs.get("url", ""), "")
            if cmd[1:3] == ["pr", "comment"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            body = Path(cmd[cmd.index("--body-file") + 1]).read_text()
            gh_calls.append(["body", body])
            open_prs["url"] = "https://github.com/o/r/pull/7"
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/r/pull/7\n", "")
        return subprocess.run(cmd, **kwargs)

    return work, origin, Publisher(run=run), gh_calls


def commit(work, name, text):
    (work / name).write_text(text)
    git(work, "add", name)
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"add {name}")


def test_pushes_the_branch_and_opens_the_pr(setup):
    work, origin, publisher, gh_calls = setup
    commit(work, "hello.txt", "hi\n")
    url = publisher.publish(work, "jef/demo/task-1", "main", "Say hi", "## What this does\nSays hi.")
    assert url == "https://github.com/o/r/pull/7"
    branches = subprocess.run(["git", "branch"], cwd=origin, capture_output=True, text=True).stdout
    assert "jef/demo/task-1" in branches
    create = next(c for c in gh_calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--base") + 1] == "main" and create[create.index("--title") + 1] == "Say hi"
    assert ["body", "## What this does\nSays hi."] in gh_calls
    # Ready again after more commits: the push updates the same PR.
    commit(work, "more.txt", "more\n")
    assert publisher.publish(work, "jef/demo/task-1", "main", "Say hi", "x") == url
    assert sum(1 for c in gh_calls if c[:3] == ["gh", "pr", "create"]) == 1


def test_a_branch_rebased_after_a_conflict_goes_to_the_same_pr(setup, tmp_path):
    work, origin, publisher, gh_calls = setup
    commit(work, "footer.txt", "version 1\n")
    url = publisher.publish(work, "jef/demo/task-1", "main", "Footer", "b")
    # Someone else's change to the same file is merged into main.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    commit(other, "footer.txt", "version two\n")
    git(other, "push", "-q", "origin", "main")
    # The implementer rebases, resolves the conflict and carries on, as Jef asks.
    git(work, "fetch", "-q", "origin")
    rebase = subprocess.run(["git", "rebase", "origin/main"], cwd=work, capture_output=True, text=True)
    assert rebase.returncode != 0  # the conflict
    (work / "footer.txt").write_text("version two, and 1\n")
    git(work, "add", "footer.txt")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.editor=true", "rebase", "--continue")
    # The rewritten branch is force-pushed (with lease) to the same PR.
    assert publisher.publish(work, "jef/demo/task-1", "main", "Footer", "b") == url
    assert sum(1 for c in gh_calls if c[:3] == ["gh", "pr", "create"]) == 1
    pushed = subprocess.run(["git", "show", "jef/demo/task-1:footer.txt"], cwd=origin, capture_output=True, text=True).stdout
    assert pushed == "version two, and 1\n"
    publisher.comment(work, url, "Jef: rebased on main to fix merge conflicts.")
    assert ["gh", "pr", "comment", url, "--body", "Jef: rebased on main to fix merge conflicts."] in gh_calls
    with pytest.raises(PublishError):
        publisher.comment(work, url, FAKE_KEY)


def test_refuses_to_push_secrets(setup):
    work, origin, publisher, gh_calls = setup
    commit(work, "config.py", f"KEY = '{FAKE_KEY}'\n")
    with pytest.raises(PublishError) as err:
        publisher.publish(work, "jef/demo/task-1", "main", "t", "b")
    assert "Anthropic API key in config.py" in str(err.value)
    assert FAKE_KEY not in str(err.value)
    branches = subprocess.run(["git", "branch"], cwd=origin, capture_output=True, text=True).stdout
    assert "jef/demo/task-1" not in branches
    assert gh_calls == []


def test_refuses_secrets_in_the_pr_text(setup):
    work, origin, publisher, gh_calls = setup
    commit(work, "ok.txt", "fine\n")
    with pytest.raises(PublishError, match="PR text"):
        publisher.publish(work, "jef/demo/task-1", "main", "t", f"token ghp_{'a' * 36}")


def test_refuses_uncommitted_work_no_commits_and_wrong_branches(setup):
    work, origin, publisher, gh_calls = setup
    with pytest.raises(PublishError, match="no commits"):
        publisher.publish(work, "jef/demo/task-1", "main", "t", "b")
    (work / "loose.txt").write_text("x")
    with pytest.raises(PublishError, match="uncommitted"):
        publisher.publish(work, "jef/demo/task-1", "main", "t", "b")
    with pytest.raises(PublishError, match="only pushes its own"):
        publisher.publish(work, "main", "main", "t", "b")
    with pytest.raises(PublishError, match="not your branch"):
        publisher.publish(work, "jef/demo/task-2", "main", "t", "b")


def test_secret_scan_only_looks_at_added_lines():
    diff = f"+++ b/a.py\n-old = '{FAKE_KEY}'\n+new = 1\n+++ b/b.py\n+k = '{FAKE_KEY}'\n"
    assert find_secrets_in_diff(diff) == [("Anthropic API key", "b.py")]


def test_refuses_a_secret_that_a_later_commit_deleted(setup):
    work, origin, publisher, gh_calls = setup
    commit(work, ".env", f"KEY={FAKE_KEY}\n")
    git(work, "rm", "-q", ".env")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "remove .env")
    commit(work, "hello.txt", "hi\n")  # the branch's final diff is clean
    with pytest.raises(PublishError) as err:
        publisher.publish(work, "jef/demo/task-1", "main", "t", "b")
    assert "Anthropic API key in .env" in str(err.value)
    assert FAKE_KEY not in str(err.value)
    branches = subprocess.run(["git", "branch"], cwd=origin, capture_output=True, text=True).stdout
    assert "jef/demo/task-1" not in branches and gh_calls == []
