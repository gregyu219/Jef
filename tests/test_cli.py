import subprocess
import sys

import pytest

from jef import cli
from jef.background import Tmux


class FakeTmux:
    """Records tmux commands instead of running them."""

    def __init__(self, running=False, port="8765"):
        self.running = running
        self.port_value = port
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[1] == "has-session":
            code = 0 if self.running else 1
            return subprocess.CompletedProcess(cmd, code, "", "")
        if cmd[1] == "show-options":
            return subprocess.CompletedProcess(cmd, 0, self.port_value + "\n", "")
        if cmd[1] == "new-session":
            self.running = True
        if cmd[1] == "kill-session":
            self.running = False
        return subprocess.CompletedProcess(cmd, 0, "", "")


def make_tmux(fake, have=("tmux", "caffeinate"), env=None):
    return Tmux(runner=fake, which=lambda name: f"/bin/{name}" if name in have else None, env=env or {})


@pytest.fixture(autouse=True)
def no_network_or_browser(monkeypatch):
    monkeypatch.setattr(cli, "is_up", lambda port: False)
    monkeypatch.setattr(cli, "wait_until_up", lambda port, seconds=15: True)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: None)


def test_start_runs_jef_in_tmux_with_caffeinate(capsys):
    fake = FakeTmux()
    assert cli.main(["start", "--port", "9001"], tmux=make_tmux(fake)) == 0
    new = next(c for c in fake.calls if c[1] == "new-session")
    assert new[:5] == ["tmux", "new-session", "-d", "-s", "jef"]
    inner = new[new.index("--") + 1 :]
    assert inner[:2] == ["caffeinate", "-is"]
    assert inner[2:] == [sys.executable, "-m", "jef", "run", "--no-browser", "--port", "9001"]
    assert ["tmux", "set-option", "-t", "=jef", "@jef_port", "9001"] in fake.calls
    assert "running in the background at http://127.0.0.1:9001" in capsys.readouterr().out


def test_start_passes_jef_home_into_the_session():
    fake = FakeTmux()
    cli.main(["start"], tmux=make_tmux(fake, env={"JEF_HOME": "/tmp/jef-test"}))
    new = next(c for c in fake.calls if c[1] == "new-session")
    assert new[new.index("-e") + 1] == "JEF_HOME=/tmp/jef-test"


def test_start_without_caffeinate_still_works():
    fake = FakeTmux()
    cli.main(["start"], tmux=make_tmux(fake, have=("tmux",)))
    new = next(c for c in fake.calls if c[1] == "new-session")
    assert new[new.index("--") + 1] == sys.executable


def test_start_without_tmux_explains_how_to_install_it(capsys):
    fake = FakeTmux()
    assert cli.main(["start"], tmux=make_tmux(fake, have=())) == 1
    assert "brew install tmux" in capsys.readouterr().out
    assert fake.calls == []


def test_start_when_already_running_does_nothing(capsys):
    fake = FakeTmux(running=True, port="9002")
    assert cli.main(["start"], tmux=make_tmux(fake)) == 0
    assert "already running at http://127.0.0.1:9002" in capsys.readouterr().out
    assert not any(c[1] == "new-session" for c in fake.calls)


def test_start_refuses_a_busy_port(monkeypatch, capsys):
    monkeypatch.setattr(cli, "is_up", lambda port: True)
    fake = FakeTmux()
    assert cli.main(["start"], tmux=make_tmux(fake)) == 1
    assert "already in use" in capsys.readouterr().out


def test_status(monkeypatch, capsys):
    assert cli.main(["status"], tmux=make_tmux(FakeTmux())) == 1
    assert "not running" in capsys.readouterr().out
    monkeypatch.setattr(cli, "is_up", lambda port: port == 9003)
    assert cli.main(["status"], tmux=make_tmux(FakeTmux(running=True, port="9003"))) == 0
    assert "running at http://127.0.0.1:9003" in capsys.readouterr().out


def test_stop(capsys):
    fake = FakeTmux(running=True)
    assert cli.main(["stop"], tmux=make_tmux(fake)) == 0
    assert ["tmux", "kill-session", "-t", "=jef"] in fake.calls
    assert "Jef stopped" in capsys.readouterr().out


def test_plain_jef_means_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_run", lambda args: seen.update(port=args.port) or 0)
    assert cli.main([]) == 0
    assert cli.main(["--port", "9100"]) == 0
    assert seen["port"] == 9100
