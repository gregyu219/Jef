import pytest
from fastapi.testclient import TestClient

from jef.backlog import Backlog, BacklogStatus
from jef.chats import Chats, Role, Status, load_roster
from jef.claude import ClaudeCheck
from jef.github import GitHubStatus
from jef.projects import StartError, load_project, parse_repo, slugify, start_project
from jef.web.app import create_app
from jef.workspace import Workspace


def alphabetical(names):
    return sorted(names)[0]


class FakeProc:
    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs, self.done = cmd, kwargs, False
        self.stdin_text = kwargs["stdin"].read().decode()

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.done = True


@pytest.fixture
def env(tmp_path):
    ws = Workspace(tmp_path / "jef")
    backlog = Backlog()
    backlog.add("Weekly email summary")
    backlog.add("Dark mode")
    backlog.save(ws.backlog_file)
    procs, clones = [], []

    def popen(cmd, **kwargs):
        procs.append(FakeProc(cmd, **kwargs))
        return procs[-1]

    def clone(repo, dest):
        clones.append(repo)
        dest.mkdir(parents=True)

    chats = Chats(ws, "/bin/claude", popen=popen)
    return ws, chats, clone, procs, clones


def test_parse_repo_accepts_names_and_links():
    assert parse_repo("gregyu219/Jef") == "gregyu219/Jef"
    assert parse_repo(" https://github.com/gregyu219/Jef.git/ ") == "gregyu219/Jef"
    assert parse_repo("git@github.com:gregyu219/Jef.git") == "gregyu219/Jef"
    for bad in ["", "Jef", "a/b/c", "../etc", "owner/..", "owner/name; rm -rf /"]:
        with pytest.raises(StartError):
            parse_repo(bad)


def test_slugify_avoids_clashes():
    assert slugify("Weekly email summary!", set()) == "weekly-email-summary"
    assert slugify("Dark mode", {"dark-mode"}) == "dark-mode-2"


def test_starting_a_project(env):
    ws, chats, clone, procs, clones = env
    project = start_project(ws, 1, "gregyu219/Jef", chats, clone=clone, choose_name=alphabetical)
    assert (project.slug, project.coordinator) == ("weekly-email-summary", "Ada")
    assert clones == ["gregyu219/Jef"]
    folder = ws.project_dir(project.slug)
    assert "Repo: gregyu219/Jef" in (folder / "goal.md").read_text()
    assert (folder / "tasks.md").exists()
    # The coordinator chat runs in the clone and follows the coordinator skill.
    [agent] = load_roster(ws, project.slug)
    assert (agent.name, agent.role, agent.status) == ("ada", Role.COORDINATOR, Status.WORKING)
    assert procs[0].kwargs["cwd"] == str((folder / "repo").resolve())
    assert "project coordinator" in procs[0].cmd[procs[0].cmd.index("--append-system-prompt") + 1]
    assert "Weekly email summary" in procs[0].stdin_text
    item = Backlog.load(ws.backlog_file).items[0]
    assert (item.status, item.coordinator) == (BacklogStatus.IN_PROGRESS, "Ada")
    assert load_project(ws, project.slug).repo == "gregyu219/Jef"
    # The next project gets the next name, and an item can't start twice.
    assert start_project(ws, 2, "gregyu219/Jef", chats, clone=clone, choose_name=alphabetical).coordinator == "Alan"
    with pytest.raises(StartError, match="already been started"):
        start_project(ws, 1, "gregyu219/Jef", chats, clone=clone, choose_name=alphabetical)


def test_a_failed_clone_leaves_nothing_behind(env):
    ws, chats, _, procs, _ = env

    def broken(repo, dest):
        raise StartError("Couldn't clone gregyu219/nope: not found")

    with pytest.raises(StartError, match="not found"):
        start_project(ws, 1, "gregyu219/nope", chats, clone=broken)
    assert ws.project_slugs() == []
    assert procs == []
    assert Backlog.load(ws.backlog_file).items[0].status is BacklogStatus.WAITING


@pytest.fixture
def client(env):
    ws, chats, clone, procs, clones = env
    app = create_app(
        ws,
        github_check=lambda: GitHubStatus(True, "octocat"),
        claude_status=ClaudeCheck(True, path="/bin/claude"),
        chats=chats,
        clone=clone,
        choose_name=alphabetical,
    )
    return TestClient(app)


def test_start_from_the_web_needs_confirmation(env, client):
    ws, chats, clone, procs, clones = env
    home = client.get("/").text
    assert 'href="/backlog/1/start"' in home
    form = client.get("/backlog/1/start").text
    assert "octocat" in form and "Yes, start this project" in form
    page = client.post("/backlog/1/start", data={"repo": "gregyu219/Jef"}).text
    assert "Tick the box" in page
    assert procs == [] and clones == []
    page = client.post("/backlog/1/start", data={"repo": "nonsense", "confirm": "yes"}).text
    assert "owner/name" in page and 'value="nonsense"' in page


def test_start_opens_the_project_page_and_chat(env, client):
    ws, chats, clone, procs, clones = env
    page = client.post("/backlog/1/start", data={"repo": "gregyu219/Jef", "confirm": "yes"})
    assert page.url.path == "/projects/weekly-email-summary"
    assert "Chat with Ada" in page.text
    assert 'http-equiv="refresh"' in page.text  # still working, so it refreshes
    assert "Weekly email summary" in page.text
    home = client.get("/").text
    assert 'href="/projects/weekly-email-summary">Ada</a>' in home
    # Ada finishes her turn; now you can message her.
    procs[0].done = True
    page = client.get("/projects/weekly-email-summary").text
    assert 'http-equiv="refresh"' not in page
    client.post("/projects/weekly-email-summary/message", data={"message": "Use plain text emails"})
    assert procs[1].stdin_text == "Use plain text emails"
    assert "--resume" in procs[1].cmd
    busy = client.post("/projects/weekly-email-summary/message", data={"message": "And another thing"}).text
    assert "still working on the last message" in busy


def test_chat_scrolls_and_keeps_its_place(env, client):
    page = client.post("/backlog/1/start", data={"repo": "gregyu219/Jef", "confirm": "yes"}).text
    assert 'class="chat"' in page
    assert "overflow-y: auto" in page  # the chat is its own scrolling box
    assert "c.scrollHeight" in page  # opens at the newest message
    assert "sessionStorage" in page  # and remembers your place across refreshes


def test_stop_from_the_web(env, client):
    ws, chats, clone, procs, clones = env
    client.post("/backlog/1/start", data={"repo": "gregyu219/Jef", "confirm": "yes"})
    page = client.post("/projects/weekly-email-summary/stop").text
    assert "Ada has been stopped" in page
    assert procs[0].done


def test_start_is_blocked_when_claude_code_does_not_work(env):
    ws, chats, clone, procs, clones = env
    app = create_app(ws, github_check=lambda: GitHubStatus(True, "o"), claude_status=ClaudeCheck(False, "missing"), clone=clone)
    page = TestClient(app).post("/backlog/1/start", data={"repo": "gregyu219/Jef", "confirm": "yes"}).text
    assert "Claude Code isn't working yet" in page
    assert clones == []


def test_unknown_projects_and_traversal_go_home(client):
    assert client.get("/projects/nope").url.path == "/"
    sneaky = client.get("/projects/..%2F..%2Fetc")
    assert sneaky.status_code == 404 or sneaky.url.path == "/"


def test_coordinator_names_are_random_among_unused(env):
    from jef.projects import COORDINATOR_NAMES, pick_coordinator_name

    backlog = Backlog()
    seen = set()
    for _ in range(200):
        seen.add(pick_coordinator_name(backlog))
    assert len(seen) > 10  # not always the same name
    assert {"Taryn", "Joseph", "Serena", "David"} <= set(COORDINATOR_NAMES)
    for name in COORDINATOR_NAMES[:-1]:
        backlog.add(name)
        backlog.items[-1].coordinator = name
    assert pick_coordinator_name(backlog) == COORDINATOR_NAMES[-1]
    backlog.add("one more")
    backlog.items[-1].coordinator = COORDINATOR_NAMES[-1]
    assert pick_coordinator_name(backlog) == f"Coordinator {len(COORDINATOR_NAMES) + 1}"


def test_start_page_remembers_repos_and_mentions_other_projects(env, client):
    ws, chats, clone, procs, clones = env
    assert "Repos you've used before" not in client.get("/backlog/1/start").text
    client.post("/backlog/1/start", data={"repo": "gregyu219/Jef", "confirm": "yes"})
    page = client.get("/backlog/2/start").text
    assert 'value="gregyu219/Jef"' in page  # filled in for you
    assert "Repos you've used before" in page
    assert "already part of 1 other project" in page and "Weekly email summary" in page
    # The new coordinator is told about the other project on the same repo.
    client.post("/backlog/2/start", data={"repo": "https://github.com/gregyu219/Jef", "confirm": "yes"})
    message = procs[-1].stdin_text
    assert "already part of other Jef projects" in message
    assert "Weekly email summary (coordinator Ada, In progress)" in message


def test_a_deleted_done_items_number_and_name_stay_with_its_project(env, client):
    from jef.projects import projects_on_repo, set_goal_field, used_backlog_numbers

    ws, chats, clone, procs, clones = env
    # Item #2 (the highest number) is started, finishes, and its row is deleted.
    done = start_project(ws, 2, "gregyu219/Jef", chats, clone=clone, choose_name=alphabetical)
    assert done.coordinator == "Ada"
    set_goal_field(ws, done.slug, "Done", "yes")
    backlog = Backlog.load(ws.backlog_file)
    backlog.items = [i for i in backlog.items if i.number != 2]
    backlog.save(ws.backlog_file)
    assert used_backlog_numbers(ws) == {2}
    # Its status still reads Done, from goal.md.
    assert projects_on_repo(ws, "gregyu219/Jef") == [(load_project(ws, done.slug), "Done")]
    # A new item doesn't take #2 back.
    client.post("/backlog", data={"feature": "Dark mode again"})
    assert [i.number for i in Backlog.load(ws.backlog_file).items] == [1, 3]
    # And a new coordinator doesn't take Ada's name back.
    assert start_project(ws, 1, "gregyu219/Jef", chats, clone=clone, choose_name=alphabetical).coordinator == "Alan"
    assert "Dark mode (coordinator Ada, Done)" in procs[-1].stdin_text


def test_coordinator_names_used_by_project_folders_are_taken():
    from jef.projects import COORDINATOR_NAMES, pick_coordinator_name

    taken = set(COORDINATOR_NAMES[:-1])
    assert pick_coordinator_name(Backlog(), taken=taken) == COORDINATOR_NAMES[-1]
    taken = set(COORDINATOR_NAMES) | {f"Coordinator {len(COORDINATOR_NAMES) + 1}"}
    assert pick_coordinator_name(Backlog(), taken=taken) == f"Coordinator {len(COORDINATOR_NAMES) + 2}"


def test_a_repo_used_by_no_other_project_isnt_mentioned(env):
    ws, chats, clone, procs, clones = env
    start_project(ws, 1, "gregyu219/Jef", chats, clone=clone, choose_name=alphabetical)
    start_project(ws, 2, "gregyu219/other", chats, clone=clone, choose_name=alphabetical)
    assert "already part of" not in procs[-1].stdin_text
