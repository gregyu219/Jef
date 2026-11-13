"""Using Jef from Telegram.

A small bot runs inside Jef as one more background thread. It asks Telegram
for new messages with long polling (``getUpdates``), so nothing on this
computer has to be reachable from the internet: only outgoing HTTPS.

It only ever answers its owner. A bot starts unlinked: the Settings page
shows a pairing code, and the first person to send ``/start <code>`` in a
private chat becomes the owner. Everyone else gets "This bot is private."

The bot's token is a secret, so its settings live outside the workspace
(agents read ``~/.jef``), in a file only you can read: see ``config_path``.

Commands call the same functions the web app does (``jef.actions``), so
Jef's rules hold whichever way you ask. A plain message goes to Jef's front
desk chat (a Claude Code turn, the same as the home page's chat), or, after
``/to <coordinator>``, to that coordinator (like the project page's message
box). Its answer is sent back once the turn ends. ``/approve``, ``/wake`` and
``/retry`` press the project page's buttons. Nothing calls GitHub.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from jef import actions, jefchat
from jef.backlog import Backlog, BacklogStatus
from jef.chats import ChatError, Chats, Reply, Status
from jef.pipeline import PipelineError
from jef.projects import Project

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MESSAGE_LIMIT = 4096  # Telegram refuses longer messages
POLL_SECONDS = 50  # how long one getUpdates call waits for news
NO_TOKEN_SECONDS = 5  # how often to look again while there's no token
BACKOFF_SECONDS = (5, 30, 60)  # waits after network errors, in turn
CONFIG_FILE = "telegram.json"
STATE_FILE = "telegram-state.json"
SECRETS_VAR = "JEF_SECRETS"  # a different folder for the token (handy for tests)

PRIVATE = "This bot is private."
UNPAIRED = (
    "This bot is private. To link it to your Jef, send /start followed by the "
    "pairing code on Jef's Settings page."
)
PAIRED = "Linked. This chat is now the only one this bot answers. Send /help to see what it can do."
HELP = """Jef on Telegram. You can send:

Any message  chat with Jef, the front desk (or the coordinator you picked with /to); the answer comes back here
/to <coordinator>  talk to a project's coordinator, like /to Radia; /to jef goes back to Jef
/to <coordinator> <message>  send one message to a coordinator
/approve <coordinator>  approve the tasks it proposed
/wake <coordinator>  ask it to check on its project now
/retry <coordinator>  clear the problem that stopped its project and try again
/reset  start the chat with Jef over
/add <feature>  add something to the backlog
/status  how each project is doing
/backlog  everything you've asked for, and what needs you
/help  this list

With /to set, /approve, /wake and /retry can leave out the name."""
JEF_BUSY = "Jef is still answering your last message. Try again when it's done."
RESET = "Starting Jef over. It'll say hello here in a moment."
ADD_USAGE = "Say what you'd like, like this: /add Dark mode for the settings page"
WATCH_POLL_SECONDS = 3  # how long getUpdates waits while a reply is due
NO_REPLY = "{label}'s turn ended without a reply."
FAILED_REPLY = "{label}'s turn failed: {text}"
JEF_NAME = "jef"  # /to jef: back to the front desk
TALKING_TO = "Now talking to {name}. Your messages go to {name} until you send /to jef."
BACK_TO_JEF = "Back to Jef. Your messages go to Jef, the front desk."
TO_USAGE = "Say who, like this: /to Radia (or /to Radia <message> for one message)."
NO_COORDINATORS = "There are no coordinators yet. Start a project from the backlog first."
COORDINATOR_BUSY = "{name} is still working. Try again when it's done."
COORDINATOR_GONE = "{name} isn't around any more (its project may have been deleted). Your messages go to Jef again."
PICK_COORDINATOR = "Say which coordinator, like this: /{command} {example}"
DONE = {
    "approve": "Approved {name}'s tasks.",
    "wake": "Asked {name} to check on the project now.",
    "retry": "Cleared the problem in {name}'s project. Jef is trying again.",
}
NOTHING_TO_RETRY = "Nothing has stopped {name}'s project, so there's nothing to try again."
ANSWER_COMING = " {name}'s answer will come here."


# --- Settings ----------------------------------------------------------------


def config_dir() -> Path:
    """Where the token is kept: outside the workspace, so no agent reads it.
    Set ``JEF_SECRETS`` to use a different folder."""
    override = os.environ.get(SECRETS_VAR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "jef"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jef"


def config_path() -> Path:
    return config_dir() / CONFIG_FILE


def new_pairing_code() -> str:
    """Hard to guess, and fits a t.me/<bot>?start=<code> link."""
    return secrets.token_hex(6)


@dataclass
class TelegramConfig:
    token: str = ""
    chat_id: int | None = None  # the owner's chat, once paired
    pairing_code: str = ""  # cleared once paired
    username: str = ""  # the bot's @username, from getMe, for the Settings page

    @property
    def paired(self) -> bool:
        return self.chat_id is not None

    @classmethod
    def new(cls, token: str, username: str = "") -> "TelegramConfig":
        """A bot that isn't linked to anyone yet, with a fresh pairing code."""
        return cls(token=token.strip(), pairing_code=new_pairing_code(), username=username)


def load_config(path: Path) -> TelegramConfig | None:
    """The saved settings, or None if there are none (or they're unreadable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        return None
    chat_id = data.get("chat_id")

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    return TelegramConfig(
        token=data["token"],
        chat_id=chat_id if isinstance(chat_id, int) else None,
        pairing_code=text("pairing_code"),
        username=text("username"),
    )


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` so only you can read it (mode 0600), replacing the file
    in one step so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_config(path: Path, config: TelegramConfig) -> None:
    _write_private(path, json.dumps(asdict(config), indent=2) + "\n")


def forget_config(path: Path) -> None:
    """Unlink the bot: remove its token and owner, and what the link kept
    about it (its offset belongs to that bot alone)."""
    path.unlink(missing_ok=True)
    path.with_name(STATE_FILE).unlink(missing_ok=True)


# --- Talking to Telegram -----------------------------------------------------


class TelegramError(RuntimeError):
    """A Bot API call failed. The message never contains the token."""


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Cut ``text`` into messages Telegram accepts, at line breaks when it can."""
    text = text.strip() or "(empty)"
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return [p for p in parts if p]


Opener = Callable[..., Any]


class Bot:
    """The three Bot API calls Jef needs, with the standard library only.
    ``opener`` is ``urllib.request.urlopen``; tests pass a fake."""

    def __init__(self, token: str, opener: Opener = urllib.request.urlopen):
        self.token = token
        self.opener = opener

    def _call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> Any:
        request = urllib.request.Request(
            f"{API}/bot{self.token}/{method}",
            data=json.dumps(params or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as err:
            # Telegram explains in the body ({"ok": false, "description": ...}).
            try:
                description = json.loads(err.read()).get("description", "")
            except Exception:
                description = ""
            raise TelegramError(f"Telegram said {err.code}: {description or err.reason}") from None
        except (OSError, ValueError) as err:
            # Not str(err) with the URL in it: the URL holds the token.
            raise TelegramError(f"Couldn't reach Telegram ({type(err).__name__})") from None
        try:
            data = json.loads(body)
        except ValueError:
            raise TelegramError("Telegram sent something that isn't JSON") from None
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description", "") if isinstance(data, dict) else ""
            raise TelegramError(f"Telegram said no: {description or 'no reason given'}")
        return data.get("result")

    def get_me(self) -> dict:
        """Who the bot is; a quick way to check a token works."""
        return self._call("getMe")

    def get_updates(self, offset: int | None = None, timeout: int = POLL_SECONDS) -> list[dict]:
        """New messages, waiting up to ``timeout`` seconds for one."""
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params, timeout=timeout + 10)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> None:
        """Plain text (no formatting, so nothing needs escaping), split if long."""
        for part in split_text(text):
            self._call("sendMessage", {"chat_id": chat_id, "text": part, "disable_web_page_preview": True})


def save_token(path: Path, token: str, opener: Opener = urllib.request.urlopen) -> TelegramConfig:
    """Check ``token`` with getMe and, if Telegram knows it, save it as a new,
    unlinked bot with a fresh pairing code (replacing any earlier bot).
    Raises ``TelegramError`` if the token is empty or doesn't work."""
    token = token.strip()
    if not token or any(c.isspace() for c in token) or "/" in token:
        raise TelegramError("That doesn't look like a bot token. Copy it from @BotFather.")
    me = Bot(token, opener).get_me()
    username = me.get("username", "") if isinstance(me, dict) else ""
    config = TelegramConfig.new(token, username=username if isinstance(username, str) else "")
    forget_config(path)  # a new bot starts afresh: Telegram counts updates per bot
    save_config(path, config)
    return config


# --- What the commands say ---------------------------------------------------


def status_text(ws) -> str:
    """One short block per project, from status.md: no LLM call."""
    summaries = actions.summarize_projects(ws)
    if not summaries:
        return "No projects yet. Add one to the backlog in Jef, then start it."
    lines = []
    for s in summaries:
        line = s.name
        details = [s.phase] if s.phase else []
        if s.total:
            details.append(f"{s.merged} of {s.total} tasks merged")
        if s.coordinator:
            details.append(s.coordinator + (f" ({s.coordinator_status})" if s.coordinator_status else ""))
        if details:
            line += "\n  " + " · ".join(details)
        if s.problem:
            line += f"\n  Problem: {s.problem}"
        lines.append(line)
    return "\n\n".join(lines)


def backlog_text(ws) -> str:
    """Every backlog item, with what's waiting on you."""
    backlog = Backlog.load(ws.backlog_file)
    if not backlog.items:
        return "The backlog is empty."
    attention = actions.backlog_attention(ws, backlog)
    lines = []
    for item in backlog.items:
        line = f"#{item.number} {item.feature} · {item.status.value}"
        if item.status is BacklogStatus.IN_PROGRESS and item.coordinator:
            line += f" ({item.coordinator})"
        if item.number in attention:
            need = attention[item.number][1]
            line += f"\n  Needs you: {need.reason}" + (f": {need.detail}" if need.detail else "")
            if need.link:
                line += f"\n  {need.link}"
        lines.append(line)
    return "\n".join(lines)


def reply_text(label: str, reply: Reply) -> str:
    """How an agent's finished turn reads on Telegram: "Jef: ..."."""
    if reply.is_error:
        if "budget" in reply.subtype:
            return FAILED_REPLY.format(label=label, text="it used up the rest of the budget.")
        return FAILED_REPLY.format(label=label, text=reply.text or reply.subtype or "no reason given.")
    return f"{label}: {reply.text.strip() or '(no text)'}"


@dataclass
class Watch:
    """A message sent from Telegram whose answer should be sent back: the
    agent's chat, and how many replies it had before the message."""

    chats: Chats
    project: str
    name: str
    label: str  # who it is, as the person knows it: "Jef", "Radia"
    seen: int

    def same_agent(self, other: "Watch") -> bool:
        return self.chats is other.chats and (self.project, self.name) == (other.project, other.name)


def parse_command(text: str) -> tuple[str, str]:
    """"/Status@JefBot now" -> ("status", "now"); plain text -> ("", text)."""
    text = text.strip()
    if not text.startswith("/"):
        return "", text
    word, _, rest = text.partition(" ")
    return word[1:].split("@", 1)[0].lower(), rest.strip()


# --- The link ----------------------------------------------------------------


class TelegramLink:
    """Jef's Telegram bot, in a background thread while Jef runs, like the
    heartbeat. It re-reads its settings each pass, so a token saved (or
    forgotten) on the Settings page takes effect without a restart."""

    def __init__(
        self,
        runtime,
        path: Path | None = None,
        state_path: Path | None = None,
        opener: Opener = urllib.request.urlopen,
        poll_seconds: int = POLL_SECONDS,
    ):
        self.runtime = runtime
        self.path = path or config_path()
        self.state_path = state_path or self.path.with_name(STATE_FILE)
        self.opener = opener
        self.poll_seconds = poll_seconds
        self._bot: Bot | None = None
        self._failures = 0
        # Answers still due. Only this thread touches them. Kept in memory
        # only: when Jef stops, the turns they wait on are ended too.
        self._watches: list[Watch] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="jef-telegram", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    @property
    def workspace(self):
        return self.runtime.workspace

    # The offset, kept so a restart doesn't answer old messages again, and
    # which coordinator /to picked, so a restart doesn't forget it.

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict) -> None:
        _write_private(self.state_path, json.dumps(state, indent=2) + "\n")

    def _update_state(self, **changes) -> None:
        """Change some keys (None removes one), keeping the rest as saved."""
        state = self._load_state()
        for key, value in changes.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        self._save_state(state)

    def talking_to(self) -> str:
        """The coordinator plain messages go to, or "" for Jef."""
        name = self._load_state().get("talking_to")
        return name if isinstance(name, str) else ""

    def bot_for(self, config: TelegramConfig) -> Bot:
        if self._bot is None or self._bot.token != config.token:
            self._bot = Bot(config.token, self.opener)
        return self._bot

    def poll_once(self) -> float:
        """Fetch and answer new messages once. Returns how long to wait
        before the next pass (0 when it should poll again straight away)."""
        config = load_config(self.path)
        if config is None or not config.token:
            return NO_TOKEN_SECONDS
        bot = self.bot_for(config)
        state = self._load_state()
        offset = state.get("offset") if isinstance(state.get("offset"), int) else None
        # While an answer is due, ask Telegram often, so the answer isn't held
        # back by a long wait for news.
        timeout = min(self.poll_seconds, WATCH_POLL_SECONDS) if self._watches else self.poll_seconds
        try:
            updates = bot.get_updates(offset, timeout=timeout)
        except TelegramError as err:
            wait = BACKOFF_SECONDS[min(self._failures, len(BACKOFF_SECONDS) - 1)]
            self._failures += 1
            log.warning("Telegram: %s; trying again in %ss", err, wait)
            return wait
        self._failures = 0
        for update in updates:
            update_id = update.get("update_id")
            try:
                config = self.handle(bot, config, update)
            except Exception:  # one bad message mustn't stop the bot
                log.exception("Telegram: couldn't handle an update")
            if isinstance(update_id, int):
                self._update_state(offset=update_id + 1)
        try:
            self.send_replies(bot, config)
        except TelegramError as err:
            log.warning("Telegram: couldn't send a reply: %s", err)
        return 0

    # Answers to messages sent from here.

    def watch(self, chats: Chats, project: str, name: str, label: str, seen: int | None = None) -> None:
        """Send the agent's next reply here once its turn ends. ``seen`` is
        how many replies it already had (by default, how many it has now)."""
        if seen is None:
            seen = len(chats.replies(project, name))
        new = Watch(chats, project, name, label, seen)
        self._watches = [w for w in self._watches if not w.same_agent(new)] + [new]

    def send_replies(self, bot: Bot, config: TelegramConfig) -> None:
        """Send the owner every reply that has come in since. A watch ends
        once its turn does, with or without a reply."""
        if not config.paired:
            self._watches = []
            return
        due, self._watches = self._watches, []
        for i, w in enumerate(due):
            try:
                replies = w.chats.replies(w.project, w.name)
                if len(replies) <= w.seen:
                    agent = next((a for a in w.chats.refresh(w.project) if a.name == w.name), None)
                    if agent is not None and agent.status is Status.WORKING:
                        self._watches.append(w)  # still thinking
                        continue
                    replies = w.chats.replies(w.project, w.name)  # it may have just finished
                if len(replies) < w.seen:
                    continue  # started over elsewhere (Reset on the web page)
                if len(replies) == w.seen:
                    bot.send_message(config.chat_id, NO_REPLY.format(label=w.label))
                for reply in replies[w.seen :]:
                    bot.send_message(config.chat_id, reply_text(w.label, reply))
                    w.seen += 1
            except TelegramError:
                # Telegram can't be reached: send the rest next pass.
                if len(replies) > w.seen:
                    self._watches.append(w)
                self._watches.extend(due[i + 1 :])
                raise
            except Exception:
                log.exception("Telegram: couldn't check %s's reply", w.label)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.poll_once()
            except Exception:
                log.exception("Telegram link failed; trying again soon")
                wait = BACKOFF_SECONDS[-1]
            if wait:
                self._stop.wait(wait)

    def handle(self, bot: Bot, config: TelegramConfig, update: dict) -> TelegramConfig:
        """Answer one update. Returns the settings, changed if it paired."""
        message = update.get("message")
        if not isinstance(message, dict):
            return config
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text")
        if not isinstance(chat_id, int):
            return config
        command, rest = parse_command(text if isinstance(text, str) else "")

        if not config.paired:
            if command == "start" and chat.get("type") == "private" and self._code_matches(config, rest):
                config = replace(config, chat_id=chat_id, pairing_code="")
                save_config(self.path, config)
                log.info("Telegram: linked to its owner's chat")
                bot.send_message(chat_id, PAIRED)
            else:
                bot.send_message(chat_id, UNPAIRED)
            return config
        if chat_id != config.chat_id:
            bot.send_message(chat_id, PRIVATE)
            return config
        if not isinstance(text, str):
            return config  # a photo, sticker and so on
        reply = self.answer(command, rest)
        if reply:  # nothing yet when the answer comes from an agent's turn
            bot.send_message(chat_id, reply)
        return config

    @staticmethod
    def _code_matches(config: TelegramConfig, given: str) -> bool:
        code = config.pairing_code
        return bool(code) and hmac.compare_digest(given.strip().encode(), code.encode())

    def answer(self, command: str, rest: str) -> str:
        """The reply to the owner's message, or "" when an agent's turn will
        answer it (see ``send_replies``)."""
        if command in ("start", "help"):
            return HELP
        if command == "status":
            return status_text(self.workspace)
        if command == "backlog":
            return backlog_text(self.workspace)
        if command == "add":
            return self.add(rest)
        if command == "reset":
            return self.reset_jef()
        if command == "to":
            return self.talk_to(rest)
        if command in DONE:
            return self.coordinator_action(command, rest)
        if command:
            return f"I don't know /{command}. Send /help to see what works."
        target = self.talking_to()
        if not target:
            return self.message_jef(rest)
        project = actions.find_coordinator(self.workspace, target)
        if project is None:
            self._update_state(talking_to=None)
            return COORDINATOR_GONE.format(name=target)
        return self.message_coordinator(project, rest)

    # Coordinators.

    def coordinator_list(self) -> str:
        names = sorted(actions.coordinators(self.workspace), key=str.casefold)
        return "Coordinators: " + ", ".join(names) + "." if names else NO_COORDINATORS

    def talk_to(self, rest: str) -> str:
        """/to <name> picks who plain messages go to; /to <name> <message>
        sends just that one; /to on its own says who it is now."""
        name, _, text = rest.partition(" ")
        text = text.strip()
        if not name:
            current = self.talking_to() or "Jef"
            return f"You're talking to {current}. {TO_USAGE}\n{self.coordinator_list()}"
        if name.casefold() == JEF_NAME:
            if text:
                return self.message_jef(text)
            self._update_state(talking_to=None)
            return BACK_TO_JEF
        project = actions.find_coordinator(self.workspace, name)
        if project is None:
            return f"There's no coordinator called {name}. {self.coordinator_list()}"
        if text:
            return self.message_coordinator(project, text)
        self._update_state(talking_to=project.coordinator)
        return TALKING_TO.format(name=project.coordinator)

    def message_coordinator(self, project: Project, text: str) -> str:
        """Pass a message to a coordinator; its reply comes back later."""
        if not text:
            return ""
        runner = self.runtime.get_chats()
        seen = len(runner.replies(project.slug, project.coordinator_agent)) if runner else 0
        try:
            actions.message_coordinator(self.workspace, runner, project.slug, text, sender="telegram")
        except actions.ActionError as err:
            return str(err)
        except ChatError as err:
            if actions.chat_error_reason(err) == "busy":
                return COORDINATOR_BUSY.format(name=project.coordinator)
            return f"Couldn't reach {project.coordinator}: {err}"
        self.watch(runner, project.slug, project.coordinator_agent, project.coordinator, seen)
        return ""

    def _pick_coordinator(self, command: str, name: str) -> tuple[Project | None, str]:
        """The coordinator ``name`` means: the one /to picked if it's empty,
        or the only one there is. Otherwise (None, what to say instead)."""
        name = name.strip() or self.talking_to()
        if not name:
            found = actions.coordinators(self.workspace)
            if len(found) == 1:
                name = next(iter(found))
            elif not found:
                return None, NO_COORDINATORS
            else:
                example = min(found, key=str.casefold)
                return None, PICK_COORDINATOR.format(command=command, example=example) + "\n" + self.coordinator_list()
        project = actions.find_coordinator(self.workspace, name)
        if project is None:
            return None, f"There's no coordinator called {name}. {self.coordinator_list()}"
        return project, ""

    def coordinator_action(self, command: str, rest: str) -> str:
        """/approve, /wake and /retry: the project page's buttons."""
        project, problem = self._pick_coordinator(command, rest)
        if project is None:
            return problem
        flow = self.runtime.get_pipeline()
        if flow is None:
            return actions.NO_CLAUDE
        slug, agent_name = project.slug, project.coordinator_agent
        runner = flow.chats

        def working() -> bool:
            agent = next((a for a in runner.refresh(slug) if a.name == agent_name), None)
            return agent is not None and agent.status is Status.WORKING

        if command == "retry" and not flow.problem(slug):
            return NOTHING_TO_RETRY.format(name=project.coordinator)
        was_working = working()
        seen = len(runner.replies(slug, agent_name))
        run = {"approve": flow.approve_tasks, "wake": flow.wake_now, "retry": flow.clear_problem}[command]
        try:
            run(slug)
        except (PipelineError, ChatError) as err:
            return str(err)
        said = DONE[command].format(name=project.coordinator)
        # The action may have woken the coordinator: send its answer here too.
        if not was_working and working():
            self.watch(runner, slug, agent_name, project.coordinator, seen)
            said += ANSWER_COMING.format(name=project.coordinator)
        return said

    def message_jef(self, text: str) -> str:
        """Pass a plain message to Jef's front desk; its reply comes back later."""
        if not text:
            return ""
        runner = self.runtime.get_jef_chats()
        if runner is None:
            return actions.NO_CLAUDE
        seen = len(runner.replies(jefchat.HOME, jefchat.NAME))
        try:
            jefchat.send(runner, self.workspace, text, sender="telegram")
        except ChatError as err:
            return JEF_BUSY if "still working" in str(err) else f"Couldn't reach Jef: {err}"
        self.watch(runner, jefchat.HOME, jefchat.NAME, "Jef", seen)
        return ""

    def reset_jef(self) -> str:
        runner = self.runtime.get_jef_chats()
        if runner is None:
            return actions.NO_CLAUDE
        try:
            jefchat.reset(runner, self.workspace)
        except ChatError as err:
            return f"Couldn't start Jef over: {err}"
        self.watch(runner, jefchat.HOME, jefchat.NAME, "Jef", seen=0)  # a fresh conversation
        return RESET

    def add(self, feature: str) -> str:
        """Add to the backlog, like the form on the home page."""
        if not feature:
            return ADD_USAGE
        try:
            item = actions.add_backlog_item(self.workspace, feature)
        except ValueError:
            return ADD_USAGE
        return f"Added #{item.number}: {item.feature}. Start it from Jef's home page when you're ready."
