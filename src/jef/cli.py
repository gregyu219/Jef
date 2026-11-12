"""The ``jef`` command.

    jef run      run Jef in this terminal (Ctrl+C stops it)
    jef start    run Jef in the background with tmux, and open the browser
    jef status   is Jef running, and where?
    jef attach   watch the background session (Ctrl+B then D to leave it)
    jef stop     stop the background session

Plain ``jef`` is the same as ``jef run``.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.request
import webbrowser

from jef import __version__
from jef.background import SESSION, Tmux, TmuxMissing

HOST = "127.0.0.1"  # Local only: Jef's UI is never reachable from other machines.
DEFAULT_PORT = 8765


def url_for(port: int) -> str:
    return f"http://{HOST}:{port}"


def is_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(url_for(port), timeout=1):
            return True
    except OSError:
        return False


def wait_until_up(port: int, seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_up(port):
            return True
        time.sleep(0.3)
    return False


def cmd_run(args: argparse.Namespace) -> int:
    import uvicorn

    from jef.web.app import create_app

    url = url_for(args.port)
    print(f"Jef is running at {url}  (press Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(), host=HOST, port=args.port, log_level="warning")
    return 0


def cmd_start(args: argparse.Namespace, tmux: Tmux) -> int:
    if tmux.is_running():
        port = tmux.port() or args.port
        print(f"Jef is already running at {url_for(port)}")
        return 0
    if is_up(args.port):
        print(f"Port {args.port} is already in use. Try:  jef start --port 9000")
        return 1
    try:
        tmux.start(args.port)
    except TmuxMissing as err:
        print(err)
        return 1
    if not wait_until_up(args.port):
        print("Jef started but isn't answering yet. See what happened with:  jef attach")
        return 1
    url = url_for(args.port)
    print(f"Jef is running in the background at {url}")
    print("  jef status    check on it")
    print("  jef attach    watch it (Ctrl+B then D to leave)")
    print("  jef stop      stop it")
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def cmd_status(args: argparse.Namespace, tmux: Tmux) -> int:
    if not tmux.is_running():
        print("Jef is not running in the background. Start it with:  jef start")
        return 1
    port = tmux.port() or DEFAULT_PORT
    if is_up(port):
        print(f"Jef is running at {url_for(port)}")
        return 0
    print(f"Jef's session is open but not answering on {url_for(port)}. Look with:  jef attach")
    return 1


def cmd_attach(args: argparse.Namespace, tmux: Tmux) -> int:
    if not tmux.is_running():
        print("Jef is not running in the background. Start it with:  jef start")
        return 1
    print("Attaching. Press Ctrl+B then D to leave Jef running.")
    os.execvp("tmux", ["tmux", "attach", "-t", f"={SESSION}"])
    return 0  # not reached


def cmd_stop(args: argparse.Namespace, tmux: Tmux) -> int:
    if not tmux.is_running():
        print("Jef is not running in the background.")
        return 0
    tmux.stop()
    print("Jef stopped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jef", description="Jef: a user-friendly software factory.")
    parser.add_argument("--version", action="version", version=f"jef {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add_port(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to serve on (default: {DEFAULT_PORT})")
        p.add_argument("--no-browser", action="store_true", help="don't open the browser")

    add_port(sub.add_parser("run", help="run Jef in this terminal"))
    add_port(sub.add_parser("start", help="run Jef in the background with tmux"))
    sub.add_parser("status", help="is Jef running, and where?")
    sub.add_parser("attach", help="watch the background session")
    sub.add_parser("stop", help="stop the background session")
    return parser


def main(argv: list[str] | None = None, tmux: Tmux | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Plain "jef" (or "jef --port 9000") means "jef run".
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    tmux = tmux or Tmux()
    handlers = {
        "run": lambda: cmd_run(args),
        "start": lambda: cmd_start(args, tmux),
        "status": lambda: cmd_status(args, tmux),
        "attach": lambda: cmd_attach(args, tmux),
        "stop": lambda: cmd_stop(args, tmux),
    }
    return handlers[args.command]()


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
