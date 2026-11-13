import io
import json
import stat
import urllib.error

import pytest

from jef import actions, jefchat, telegram
from jef.backlog import Backlog, BacklogStatus
from jef.chats import Chats
from jef.runtime import Runtime
from jef.telegram import Bot, TelegramConfig, TelegramError, TelegramLink
from jef.workspace import Workspace

OWNER = 111
STRANGER = 222
TOKEN = "123456:test-token-not-real"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTelegram:
    """Stands in for urlopen: answers getUpdates from a queue and records
    every sendMessage."""

    def __init__(self):
        self.updates = []  # lists of updates, one list per getUpdates call
        self.sent = []  # (chat_id, text)
        self.calls = []  # (method, params)
        self.fail = None  # an exception to raise on the next call

    def __call__(self, request, timeout=None):
        method = request.full_url.rsplit("/", 1)[1]
        params = json.loads(request.data)
        self.calls.append((method, params))
        if self.fail is not None:
            err, self.fail = self.fail, None
            raise err
        if method == "getUpdates":
            result = self.updates.pop(0) if self.updates else []
        elif method == "sendMessage":
            self.sent.append((params["chat_id"], params["text"]))
            result = {"message_id": len(self.sent)}
        else:
            result = {"username": "JefBot"}
        return FakeResponse(json.dumps({"ok": True, "result": result}).encode())

    def texts(self, chat_id=OWNER):
        return [text for cid, text in self.sent if cid == chat_id]


def message(update_id, text, chat_id=OWNER, chat_type="private"):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


@pytest.fixture
def env(tmp_path):
    ws = Workspace(tmp_path / "jef")
    path = tmp_path / "secrets" / "telegram.json"
    fake = FakeTelegram()
    link = TelegramLink(Runtime(ws), path=path, opener=fake, poll_seconds=0)
    return ws, path, fake, link


def paired(env):
    _, path, _, _ = env
    telegram.save_config(path, TelegramConfig(token=TOKEN, chat_id=OWNER))


def say(env, *texts, chat_id=OWNER):
    """The owner (or ``chat_id``) sends ``texts``; the link answers them."""
    _, _, fake, link = env
    start = link._load_state().get("offset", 1)
    fake.updates.append([message(start + i, t, chat_id) for i, t in enumerate(texts)])
    assert link.poll_once() == 0


# --- Settings ----------------------------------------------------------------


def test_config_is_saved_only_readable_by_you(env):
    _, path, _, _ = env
    config = TelegramConfig.new(f"  {TOKEN} ")
    telegram.save_config(path, config)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = telegram.load_config(path)
    assert loaded.token == TOKEN and not loaded.paired
    assert len(loaded.pairing_code) == 12 and loaded.pairing_code == config.pairing_code
    assert TelegramConfig.new(TOKEN).pairing_code != config.pairing_code
    telegram.forget_config(path)
    assert telegram.load_config(path) is None
    telegram.forget_config(path)  # already gone: fine


def test_unreadable_config_counts_as_none(env):
    _, path, _, _ = env
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert telegram.load_config(path) is None
    path.write_text('{"chat_id": 5}')
    assert telegram.load_config(path) is None


def test_config_lives_outside_the_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("JEF_SECRETS", raising=False)
    monkeypatch.setenv("JEF_HOME", str(tmp_path / "jef"))
    assert Workspace.default().root not in telegram.config_path().parents
    monkeypatch.setenv("JEF_SECRETS", str(tmp_path / "s"))
    assert telegram.config_path() == tmp_path / "s" / "telegram.json"


# --- The Bot API -------------------------------------------------------------


def test_split_text_keeps_messages_under_the_limit():
    assert telegram.split_text("hi") == ["hi"]
    assert telegram.split_text("") == ["(empty)"]
    lines = "\n".join(f"line {i}" for i in range(1000))
    parts = telegram.split_text(lines, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts) == lines  # cut only at line breaks
    assert telegram.split_text("x" * 250, limit=100) == ["x" * 100, "x" * 100, "x" * 50]


def test_long_messages_are_sent_in_parts():
    fake = FakeTelegram()
    Bot(TOKEN, fake).send_message(OWNER, "y" * 5000)
    assert [len(t) for t in fake.texts()] == [4096, 904]
    assert all("parse_mode" not in params for _, params in fake.calls)


def test_get_updates_sends_offset_and_timeout():
    fake = FakeTelegram()
    fake.updates.append([message(7, "hi")])
    assert Bot(TOKEN, fake).get_updates(7, timeout=50)[0]["update_id"] == 7
    assert fake.calls[0] == ("getUpdates", {"timeout": 50, "allowed_updates": ["message"], "offset": 7})


def test_errors_never_show_the_token():
    fake = FakeTelegram()
    body = io.BytesIO(json.dumps({"ok": False, "description": "Unauthorized"}).encode())
    fake.fail = urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/getMe", 401, "Unauthorized", {}, body)
    with pytest.raises(TelegramError, match="401: Unauthorized") as err:
        Bot(TOKEN, fake).get_me()
    assert TOKEN not in str(err.value)
    fake.fail = urllib.error.URLError(f"no route to bot{TOKEN}")
    with pytest.raises(TelegramError, match="Couldn't reach") as err:
        Bot(TOKEN, fake).get_me()
    assert TOKEN not in str(err.value)


# --- Pairing and who it answers ----------------------------------------------


def test_does_nothing_without_a_token(env):
    _, _, fake, link = env
    assert link.poll_once() == telegram.NO_TOKEN_SECONDS
    assert fake.calls == []


def test_pairing_with_the_code_makes_you_the_owner(env):
    _, path, fake, link = env
    config = TelegramConfig.new(TOKEN, username="JefBot")
    telegram.save_config(path, config)
    say(env, "/start", "/start wrong-code", chat_id=STRANGER)
    assert fake.texts(STRANGER) == [telegram.UNPAIRED, telegram.UNPAIRED]
    assert not telegram.load_config(path).paired

    say(env, f"/start {config.pairing_code}", "/status")
    saved = telegram.load_config(path)
    assert saved.chat_id == OWNER and saved.pairing_code == ""
    assert saved.token == TOKEN and saved.username == "JefBot"  # kept for the Settings page
    assert fake.texts() == [telegram.PAIRED, "No projects yet. Add one to the backlog in Jef, then start it."]

    # The code is used up, and nobody else is answered from now on.
    say(env, f"/start {config.pairing_code}", "/status", chat_id=STRANGER)
    assert fake.texts(STRANGER)[-2:] == [telegram.PRIVATE, telegram.PRIVATE]
    assert telegram.load_config(path).chat_id == OWNER


def test_saving_a_new_token_starts_a_new_bot_afresh(env):
    _, path, fake, link = env
    paired(env)
    link._save_state({"offset": 900})
    config = telegram.save_token(path, f"  {TOKEN}  ", opener=fake)
    assert fake.calls[-1][0] == "getMe"
    assert config.username == "JefBot" and config.pairing_code and not config.paired
    assert telegram.load_config(path) == config
    assert link._load_state() == {}  # the old bot's offset would skip the new bot's messages


def test_save_token_refuses_what_isnt_a_token(env):
    _, path, fake, _ = env
    for bad in ("", "  ", "12 34", "a/b"):
        with pytest.raises(TelegramError):
            telegram.save_token(path, bad, opener=fake)
    assert fake.calls == [] and not path.exists()


def test_pairing_only_from_a_private_chat(env):
    _, path, fake, link = env
    config = TelegramConfig.new(TOKEN)
    telegram.save_config(path, config)
    fake.updates.append([message(1, f"/start {config.pairing_code}", chat_id=-5, chat_type="group")])
    link.poll_once()
    assert not telegram.load_config(path).paired


def test_offset_is_saved_so_a_restart_skips_old_messages(env):
    ws, path, fake, link = env
    paired(env)
    say(env, "/help", "/help")
    assert link._load_state()["offset"] == 3
    assert stat.S_IMODE(link.state_path.stat().st_mode) == 0o600
    again = TelegramLink(Runtime(ws), path=path, opener=fake, poll_seconds=0)
    again.poll_once()
    assert fake.calls[-1] == ("getUpdates", {"timeout": 0, "allowed_updates": ["message"], "offset": 3})


def test_network_errors_back_off_and_recover(env):
    _, _, fake, link = env
    paired(env)
    waits = []
    for _ in range(4):
        fake.fail = urllib.error.URLError("down")
        waits.append(link.poll_once())
    assert waits == [5, 30, 60, 60]
    assert link.poll_once() == 0
    fake.fail = urllib.error.URLError("down")
    assert link.poll_once() == 5


def test_a_message_that_breaks_is_skipped(env, monkeypatch):
    _, _, fake, link = env
    paired(env)
    monkeypatch.setattr(link, "answer", lambda c, r: 1 / 0)
    say(env, "/status")
    assert link._load_state()["offset"] == 2  # not answered again and again


def test_link_thread_starts_and_stops(env):
    _, _, _, link = env
    link.start()
    link.stop()
    assert not link._thread.is_alive()


# --- Commands ----------------------------------------------------------------


def test_help_and_unknown_commands(env):
    _, _, fake, _ = env
    paired(env)
    say(env, "/help", "/start", "/help@JefBot", "/nope", "hello")
    texts = fake.texts()
    assert texts[:3] == [telegram.HELP] * 3
    assert texts[3] == "I don't know /nope. Send /help to see what works."
    assert texts[4] == actions.NO_CLAUDE  # no Claude Code in this test, so no Jef chat
    for command in ("/status", "/backlog", "/reset", "/add", "/help"):
        assert command in telegram.HELP


def test_parse_command():
    assert telegram.parse_command(" /Status@JefBot  now ") == ("status", "now")
    assert telegram.parse_command("hello /status") == ("", "hello /status")


def write_project(ws, slug, status):
    folder = ws.project_dir(slug)
    folder.mkdir(parents=True)
    (folder / "tasks.md").write_text(
        "# Tasks: Dark mode\n\n| # | Task | Status | Agent | PR |\n| --- | --- | --- | --- | --- |\n"
        "| 1 | Colors | Merged | impl-1 | |\n| 2 | Toggle | Implementation | impl-2 | |\n"
    )
    (folder / "status.md").write_text(status)


def test_status_lists_each_project(env):
    ws, _, fake, _ = env
    paired(env)
    write_project(
        ws,
        "dark-mode",
        "# Status: Dark mode\n\n- Phase: Building\n- Coordinator: Ada (Working)\n"
        "- Problem: Ada is waiting for you: which blue?\n",
    )
    say(env, "/status")
    assert fake.texts()[-1] == (
        "Dark mode\n  Building · 1 of 2 tasks merged · Ada (Working)\n  Problem: Ada is waiting for you: which blue?"
    )


def test_backlog_lists_items_and_what_needs_you(env):
    ws, _, fake, _ = env
    paired(env)
    say(env, "/backlog")
    assert fake.texts()[-1] == "The backlog is empty."

    actions.add_backlog_item(ws, "Dark mode")
    actions.add_backlog_item(ws, "Weekly email")
    backlog = Backlog.load(ws.backlog_file)
    backlog.items[0].status, backlog.items[0].coordinator = BacklogStatus.IN_PROGRESS, "Ada"
    backlog.save(ws.backlog_file)
    folder = ws.project_dir("dark-mode")
    folder.mkdir(parents=True)
    (folder / "goal.md").write_text(
        "# Goal: Dark mode\n\n- Repo: a/b\n- Coordinator: Ada\n- Backlog item: #1\n"
        "- Problem: Ada is waiting for you: which blue?\n"
    )
    say(env, "/backlog")
    assert fake.texts()[-1] == (
        "#1 Dark mode · In progress (Ada)\n  Needs you: Question: which blue?\n#2 Weekly email · Waiting"
    )


# --- Chatting with Jef -------------------------------------------------------


class FakeProc:
    def __init__(self, cmd, **kwargs):
        self.cmd, self.done = cmd, False
        self.stdin_text = kwargs["stdin"].read().decode()

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.done = True


@pytest.fixture
def chat(env):
    """A paired link whose runtime has a Jef chat with fake Claude turns."""
    ws, path, fake, _ = env
    procs = []

    def popen(cmd, **kwargs):
        procs.append(FakeProc(cmd, **kwargs))
        return procs[-1]

    ids = iter(f"session-{i}" for i in range(1, 10))
    runner = Chats(jefchat.jef_workspace(ws), "/bin/claude", popen=popen, new_session_id=lambda: next(ids))
    link = TelegramLink(Runtime(ws, jef_chats=runner), path=path, opener=fake, poll_seconds=0)
    paired(env)
    return (ws, path, fake, link), runner, procs


def finish(runner, procs, text, is_error=False, subtype="success"):
    """The latest turn ends with ``text`` as its reply."""
    with runner.chat_file(jefchat.HOME, jefchat.NAME).open("a") as out:
        out.write(json.dumps({"type": "result", "result": text, "is_error": is_error, "subtype": subtype}) + "\n")
    procs[-1].done = True


def test_a_message_goes_to_jef_and_its_answer_comes_back(chat):
    env, runner, procs = chat
    _, _, fake, _ = env
    say(env, "How's it going?")
    assert fake.texts() == []  # nothing until Jef answers
    assert procs[0].stdin_text.endswith("The person says: How's it going?")
    assert '"from": "telegram"' in runner.chat_file(jefchat.HOME, jefchat.NAME).read_text()

    say(env)  # Jef is still thinking
    assert fake.texts() == []

    finish(runner, procs, "All good. Dark mode is half done.")
    say(env)
    assert fake.texts() == ["Jef: All good. Dark mode is half done."]
    say(env)
    assert len(fake.texts()) == 1  # sent once

    say(env, "And the email?")
    assert procs[1].cmd[procs[1].cmd.index("--resume") + 1] == "session-1"
    assert '"from": "telegram", "text": "And the email?"' in runner.chat_file(jefchat.HOME, jefchat.NAME).read_text()
    finish(runner, procs, "Waiting on you.")
    say(env)
    assert fake.texts() == ["Jef: All good. Dark mode is half done.", "Jef: Waiting on you."]


def test_polls_quickly_while_an_answer_is_due(chat):
    env, runner, procs = chat
    _, _, fake, link = env
    link.poll_seconds = 50
    fake.updates.append([message(1, "Hi")])
    link.poll_once()
    assert fake.calls[0][1]["timeout"] == 50
    link.poll_once()
    assert fake.calls[-1][1]["timeout"] == telegram.WATCH_POLL_SECONDS
    finish(runner, procs, "Hello")
    link.poll_once()  # sends the answer
    link.poll_once()
    assert fake.calls[-1] == ("getUpdates", {"timeout": 50, "allowed_updates": ["message"], "offset": 2})


def test_busy_jef_says_so(chat):
    env, _, _ = chat
    _, _, fake, _ = env
    say(env, "Hi", "Again")
    assert fake.texts() == [telegram.JEF_BUSY]


def test_a_failed_or_silent_turn_is_reported(chat):
    env, runner, procs = chat
    _, _, fake, _ = env
    say(env, "Hi")
    finish(runner, procs, "", is_error=True, subtype="error_max_budget_usd")
    say(env)
    assert fake.texts() == ["Jef's turn failed: it used up the rest of the budget."]
    say(env, "Again")
    procs[-1].done = True  # ended without a result line
    say(env)
    assert fake.texts()[-1] == telegram.NO_REPLY.format(label="Jef")


def test_replies_wait_while_telegram_is_down(chat):
    env, runner, procs = chat
    _, _, fake, link = env
    say(env, "Hi")
    finish(runner, procs, "Hello")

    def down_for_sending(request, timeout=None):
        if request.full_url.endswith("sendMessage"):
            raise urllib.error.URLError("down")
        return fake(request, timeout)

    link.opener, link._bot = down_for_sending, None
    assert link.poll_once() == 0
    assert fake.texts() == []
    link.opener, link._bot = fake, None
    say(env)
    assert fake.texts() == ["Jef: Hello"]


def test_reset_starts_over_and_sends_the_greeting(chat):
    env, runner, procs = chat
    ws, _, fake, _ = env
    say(env, "Hi")
    finish(runner, procs, "Hello")
    say(env)
    say(env, "/reset")
    assert fake.texts()[-1] == telegram.RESET
    assert procs[-1].stdin_text == jefchat.context_message(ws)
    finish(runner, procs, "Hi again, here's where things stand.")
    say(env)
    assert fake.texts() == ["Jef: Hello", telegram.RESET, "Jef: Hi again, here's where things stand."]


def test_add_puts_a_feature_on_the_backlog(chat):
    env, _, _ = chat
    ws, _, fake, _ = env
    say(env, "/add", "/add   Dark mode  ")
    assert fake.texts()[0] == telegram.ADD_USAGE
    assert fake.texts()[1].startswith("Added #1: Dark mode.")
    items = Backlog.load(ws.backlog_file).items
    assert [(i.number, i.feature, i.status) for i in items] == [(1, "Dark mode", BacklogStatus.WAITING)]


def test_reset_and_add_without_claude(env):
    ws, _, fake, _ = env
    paired(env)
    say(env, "/reset", "/add Dark mode")
    assert fake.texts()[0] == actions.NO_CLAUDE
    assert fake.texts()[1].startswith("Added #1")  # the backlog needs no Claude
