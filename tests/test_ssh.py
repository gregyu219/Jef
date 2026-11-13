import subprocess

from jef.ssh import KEEPALIVE, keep_ssh_alive


def git_config(value):
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0 if value else 1, stdout=value)


def test_sets_a_keepalive_when_you_havent_chosen_an_ssh_command():
    env = {}
    assert keep_ssh_alive(env, run=git_config(""))
    assert env["GIT_SSH_COMMAND"] == KEEPALIVE and "ServerAliveInterval" in KEEPALIVE


def test_keeps_your_own_ssh_command():
    env = {"GIT_SSH_COMMAND": "ssh -i ~/.ssh/work"}
    assert not keep_ssh_alive(env, run=git_config(""))
    assert env["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/work"
    env = {}
    assert not keep_ssh_alive(env, run=git_config("ssh -i ~/.ssh/work\n"))
    assert "GIT_SSH_COMMAND" not in env


def test_git_missing_is_fine():
    def broken(cmd, **kw):
        raise OSError("no git")

    env = {}
    assert keep_ssh_alive(env, run=broken)
