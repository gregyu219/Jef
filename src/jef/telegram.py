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
Jef's rules hold whichever way you ask. Nothing here calls Claude or GitHub.
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from jef import actions
from jef.backlog import Backlog, BacklogStatus

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

/status  how each project is doing
/backlog  everything you've asked for, and what needs you
/help  this list"""
NOT_YET = "Chatting with Jef from here isn't ready yet. Send /help to see what works."


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

    @property
    def paired(self) -> bool:
        return self.chat_id is not None

    @classmethod
    def new(cls, token: str) -> "TelegramConfig":
        """A bot that isn't linked to anyone yet, with a fresh pairing code."""
        return cls(token=token.strip(), pairing_code=new_pairing_code())


def load_config(path: Path) -> TelegramConfig | None:
    """The saved settings, or None if there are none (or they're unreadable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        return None
    chat_id = data.get("chat_id")
    return TelegramConfig(
        token=data["token"],
        chat_id=chat_id if isinstance(chat_id, int) else None,
        pairing_code=data.get("pairing_code") if isinstance(data.get("pairing_code"), str) else "",
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
    """Unlink the bot: remove its token and owner."""
    path.unlink(missing_ok=True)


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

    # The offset, kept so a restart doesn't answer old messages again.

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict) -> None:
        _write_private(self.state_path, json.dumps(state, indent=2) + "\n")

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
        try:
            updates = bot.get_updates(offset, timeout=self.poll_seconds)
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
                state["offset"] = update_id + 1
                self._save_state(state)
        return 0

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
                config = TelegramConfig(token=config.token, chat_id=chat_id)
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
        bot.send_message(chat_id, self.answer(command, rest))
        return config

    @staticmethod
    def _code_matches(config: TelegramConfig, given: str) -> bool:
        code = config.pairing_code
        return bool(code) and hmac.compare_digest(given.strip().encode(), code.encode())

    def answer(self, command: str, rest: str) -> str:
        """The reply to the owner's message."""
        if command in ("start", "help"):
            return HELP
        if command == "status":
            return status_text(self.workspace)
        if command == "backlog":
            return backlog_text(self.workspace)
        if command:
            return f"I don't know /{command}. Send /help to see what works."
        return NOT_YET
