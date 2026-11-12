import threading

from fastapi.testclient import TestClient

from jef.claude import ClaudeCheck
from jef.github import GitHubStatus
from jef.runtime import Runtime
from jef.web.app import create_app
from jef.workspace import Workspace


def working() -> ClaudeCheck:
    return ClaudeCheck(True, path="/bin/claude")


def test_nothing_runs_until_claude_code_works(tmp_path):
    runtime = Runtime(Workspace(tmp_path), claude_status=ClaudeCheck(False, "missing"))
    assert runtime.get_chats() is None
    assert runtime.get_jef_chats() is None
    assert runtime.get_pipeline() is None


def test_every_caller_gets_the_same_chats_and_pipeline(tmp_path):
    runtime = Runtime(Workspace(tmp_path), claude_status=working())
    pipeline = runtime.get_pipeline()
    assert pipeline is not None and pipeline is runtime.get_pipeline()
    assert runtime.get_chats() is runtime.get_chats() is pipeline.chats
    assert runtime.get_jef_chats() is runtime.get_jef_chats()
    assert runtime.get_jef_chats() is not runtime.get_chats()


def test_the_web_app_and_background_threads_share_one_runtime(tmp_path):
    runtime = Runtime(Workspace(tmp_path), claude_status=working())
    app = create_app(runtime=runtime, github_check=lambda: GitHubStatus(True, "o"))
    assert app.state.runtime is runtime
    TestClient(app).get("/")  # the home page builds Jef's chat
    seen = []
    thread = threading.Thread(target=lambda: seen.append((runtime.get_jef_chats(), runtime.get_pipeline())))
    thread.start()
    thread.join()
    assert seen == [(runtime.jef_chats, runtime.pipeline)]
    assert runtime.jef_chats is not None and runtime.pipeline is not None


def test_check_again_in_the_web_app_reaches_the_shared_runtime(tmp_path):
    runtime = Runtime(Workspace(tmp_path), claude_status=ClaudeCheck(False, "missing"))
    ready = []
    runtime.on_agents_ready(ready.append)
    app = create_app(runtime=runtime, github_check=lambda: GitHubStatus(True, "o"), claude_check=working)
    TestClient(app).post("/settings/claude-check")
    assert runtime.claude.ok and ready == [runtime.pipeline]


def test_agents_ready_runs_once_even_with_many_threads(tmp_path):
    runtime = Runtime(Workspace(tmp_path), claude_status=working())
    ready = []
    runtime.on_agents_ready(ready.append)
    threads = [threading.Thread(target=runtime.get_pipeline) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ready == [runtime.pipeline]


class FakeThread:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def stop(self):
        self.log.append(self)
        if self.fail:
            raise RuntimeError("boom")


class FakeChats:
    def __init__(self, log):
        self.log = log

    def stop_running_turns(self):
        self.log.append(self)


def test_stop_ends_threads_then_running_turns(tmp_path):
    log = []
    broken, fine = FakeThread(log, fail=True), FakeThread(log)
    chats, jef_chats = FakeChats(log), FakeChats(log)
    runtime = Runtime(Workspace(tmp_path), chats=chats, jef_chats=jef_chats)
    runtime.keep(broken)
    runtime.keep(fine)
    runtime.stop()  # one thread failing to stop doesn't stop the rest
    assert log == [broken, fine, chats, jef_chats]
    runtime.stop()
    assert log[4:] == [chats, jef_chats]  # threads are only stopped once
