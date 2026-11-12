"""The ``jef`` command: start Jef's local web UI."""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from jef import __version__
from jef.web.app import create_app

HOST = "127.0.0.1"  # Local only: Jef's UI is never reachable from other machines.


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jef", description="Start Jef's local web UI.")
    parser.add_argument("--port", type=int, default=8765, help="port to serve on (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    parser.add_argument("--version", action="version", version=f"jef {__version__}")
    args = parser.parse_args(argv)

    url = f"http://{HOST}:{args.port}"
    print(f"Jef is running at {url}  (press Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(), host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
