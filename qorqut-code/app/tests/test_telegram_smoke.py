import pytest
from telegram_support import Env


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_ready_and_poll_a_message(env):
    bridge = env.ready()
    env.api.push(env.message("founder", "hello"))
    bridge.poll_once()
    assert env.one("SELECT state FROM telegram_updates")["state"] == "done"
    assert env.one("SELECT body FROM telegram_messages")["body"].endswith("hello")
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 1
