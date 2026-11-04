from types import SimpleNamespace
import unittest

from qorqut_operations.contracts import DomainError
from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend


class Broker:
    def __init__(self, screen):
        self.screen = screen

    def tmux(self, *args):
        return 0, self.screen


def backend(snapshots, screen="› Ask Codex to do anything\n"):
    terminals = SimpleNamespace(runtime=SimpleNamespace())
    result = AgentTerminalBackend(terminals, timeout=5, sleep=lambda seconds: None)
    broker = Broker(screen)
    result.selected = lambda row: (broker, None, "session", None, None)
    sequence = iter(snapshots)

    def snapshot(row, client=None):
        item = next(sequence)
        if isinstance(item, Exception):
            raise item
        return item

    result.snapshot = snapshot
    return result


def unreadable():
    error = DomainError("operation_unavailable", "Cannot identify the foreground terminal process")
    error.__cause__ = PermissionError("Cannot inspect process 28495")
    return error


class WaitReadyTest(unittest.TestCase):
    def test_a_process_that_cannot_be_read_yet_is_observed_again(self):
        ready = {"pane_id": "%1", "command": "codex"}
        backend([unreadable(), unreadable(), ready]).wait_ready({"agent_id": "a"}, "codex")

    def test_other_failures_still_stop_the_start(self):
        refused = DomainError("operation_unavailable", "A shared app-server is not a terminal viewer")
        with self.assertRaises(DomainError) as raised:
            backend([refused]).wait_ready({"agent_id": "a"}, "codex")
        self.assertIn("app-server", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
