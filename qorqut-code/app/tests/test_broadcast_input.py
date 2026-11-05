"""Input uses the registered existing backend without preparing or starting it."""
from contextlib import contextmanager
from types import SimpleNamespace

from test_terminal_execution import AGENT_ONE, Mirror, make_terminal


def test_existing_input_selects_business_backend_and_submits_through_owning_broker(tmp_path, monkeypatch):
    terminal, (business, _), (host, broker, other), prepared, _ = make_terminal(tmp_path)
    row = terminal.agents[(business['id'], AGENT_ONE)][1]
    @contextmanager
    def read():
        yield SimpleNamespace(
            repository=lambda _: SimpleNamespace(get=lambda identity: business if identity == business['id'] else None),
            connection=SimpleNamespace(execute=lambda query, args: SimpleNamespace(
                fetchone=lambda: row if args == (AGENT_ONE, business['id']) else None)))
    monkeypatch.setattr(terminal.runtime.database, 'read', read)
    assert terminal.input_existing(business['id'], AGENT_ONE, 'Review now')['state'] == 'unavailable'
    assert not broker.ensure_calls and not broker.attach_calls
    key, session, _ = terminal.binding(business, row, broker)
    submitted = []
    broker.submit_input = lambda target, text: submitted.append((target, text))
    mirror = Mirror()
    broker.mirrors[key] = mirror
    broker.sessions.add(session)
    assert terminal.input_existing(business['id'], AGENT_ONE, 'Review now')['state'] == 'delivered'
    assert submitted == [(session, 'Review now')]
    assert not mirror.inputs
    assert not prepared and not broker.ensure_calls and not broker.attach_calls
    assert not host.mirrors and not other.mirrors
    mirror.done = True
    assert terminal.input_existing(business['id'], AGENT_ONE, 'Again')['state'] == 'unavailable'
    assert submitted == [(session, 'Review now')]
    assert not mirror.inputs
