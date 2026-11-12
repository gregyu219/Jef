import json
import subprocess

from jef import claude


def fake_run(stdout="", returncode=0, stderr="", raises=None):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run, seen


def which(name):
    return "/usr/local/bin/claude" if name == "claude" else None


def test_a_working_claude_code(monkeypatch):
    run, seen = fake_run(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
    result = claude.check(runner=run, which=which)
    assert result.ok and result.path == "/usr/local/bin/claude"
    cmd = seen["cmd"]
    assert cmd[:3] == ["/usr/local/bin/claude", "-p", claude.CHECK_PROMPT]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "haiku"
    # It never goes through bare mode, which would ignore your Claude login.
    assert "--bare" not in cmd
    assert seen["kwargs"]["cwd"] != "."


def test_missing_claude(monkeypatch):
    monkeypatch.setattr(claude.os, "access", lambda *a: False)
    result = claude.check(runner=fake_run()[0], which=lambda n: None)
    assert not result.ok and result.problem == "missing"
    assert "isn't installed" in result.help


def test_not_signed_in():
    run, _ = fake_run(json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}), returncode=1)
    result = claude.check(runner=run, which=which)
    assert not result.ok and result.problem == "failed"
    assert "Not logged in" in result.help and "sign in" in result.help


def test_garbage_output_and_timeouts():
    assert claude.check(runner=fake_run("oops", 1, "boom")[0], which=which).detail == "boom"
    timeout = claude.check(runner=fake_run(raises=subprocess.TimeoutExpired("claude", 1))[0], which=which)
    assert timeout.problem == "timeout" and "two minutes" in timeout.help
