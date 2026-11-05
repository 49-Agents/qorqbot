"""Business terminal execution selection stays scoped to one backend."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.transport import terminals as terminal_module
from bazarlyk.transport.terminals import TerminalExecution, TerminalService

AGENT_ONE = "00000000-0000-4000-8000-000000000001"
AGENT_TWO = "00000000-0000-4000-8000-000000000002"
AGENT_PENDING = "00000000-0000-4000-8000-000000000003"
AGENT_GLOBAL = "00000000-0000-4000-8000-000000000004"


class Mirror:
    cols, rows = 120, 32

    def __init__(self, *, done=False):
        self.done = done
        self.inputs = []
        self.resizes = []

    def snapshot(self):
        return b"", 0

    def tail_from(self, cursor):
        return b"", cursor

    def write(self, data):
        self.inputs.append(data)

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.resizes.append((rows, cols))


class Broker:
    def __init__(self, name):
        self.name = name
        self.sessions, self.mirrors = set(), {}
        self.ensure_calls, self.attach_calls, self.resize_calls = [], [], []
        self.kill_calls, self.stop_calls = [], []

    @staticmethod
    def tmux_available():
        return True

    def session_name(self, slug, name):
        return f"{self.name}:{slug}:{name}"

    def session_prefix(self, slug):
        return f"{self.name}:{slug}:"

    def attach_cmd(self, session):
        self.attach_calls.append(session)
        return ["attach", session]

    def ensure_session(self, session, shell, cwd, **kwargs):
        self.sessions.add(session)
        self.ensure_calls.append((session, shell, cwd, kwargs))

    def get_or_start(self, key, *args, **kwargs):
        self.attach_calls.append((key, args, kwargs))
        mirror = self.mirrors.get(key)
        if mirror is None or mirror.done:
            mirror = Mirror()
            self.mirrors[key] = mirror
        return mirror

    def list_sessions(self, prefix):
        return [session for session in self.sessions if session.startswith(prefix)]

    def get(self, key):
        return self.mirrors.get(key)

    def resize_session(self, session, rows, cols):
        self.resize_calls.append((session, rows, cols))

    def kill_session(self, session):
        self.kill_calls.append(session)
        if session not in self.sessions:
            return False
        self.sessions.remove(session)
        return True

    def stop(self, key):
        self.stop_calls.append(key)
        return self.mirrors.pop(key, None) is not None

    def session_alive(self, session):
        return session in self.sessions


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _query, params):
        return _Rows(self.rows.get(params[0], []))


class _Read:
    def __init__(self, rows):
        self.uow = SimpleNamespace(connection=_Connection(rows))

    def __enter__(self):
        return self.uow

    def __exit__(self, *args):
        return False


class Runtime:
    def __init__(self, root, rows):
        self.root = root
        self.workspace = root / "workspace"
        self.resolver = object()
        self.database = SimpleNamespace(read=lambda: _Read(rows))
        self.application = SimpleNamespace(writer_admission=None)


class ProbeTerminal(TerminalService):
    def __init__(self, runtime, broker, agents, **kwargs):
        super().__init__(runtime, broker, **kwargs)
        self.agents = agents

    def owner(self, context):
        return None

    def agent(self, context, selector):
        return self.agents[(context.business_id, selector)]


class Request:
    def __init__(self, method, body=None):
        self.method = method
        self.headers = {"content-type": "application/json", "sec-fetch-site": "same-origin"}
        self.body = json.dumps(body or {}).encode()

    async def stream(self):
        yield self.body

    async def is_disconnected(self):
        return True


def make_terminal(tmp_path):
    root = tmp_path / "root"
    (root / "agents/global-agent").mkdir(parents=True)
    business_one = {"id": "one", "slug": "one", "hq_path": str(root / "one/hq")}
    business_two = {"id": "two", "slug": "two", "hq_path": str(root / "two/hq")}
    rows = {
        "one": [{"agent_id": AGENT_ONE, "name": "one-agent", "workspace": str(root / "one/hq/agents/one-agent"),
            "home_business_id": "one"}],
        "two": [{"agent_id": AGENT_TWO, "name": "two-agent", "workspace": str(root / "two/hq/agents/two-agent"),
            "home_business_id": "two"}],
        "pending": [{"agent_id": AGENT_PENDING, "name": "pending-agent", "workspace": str(root / "pending/hq/agents/pending-agent"),
            "home_business_id": "pending"}],
        None: [{"agent_id": AGENT_GLOBAL, "name": "global-agent", "workspace": str(root / "agents/global-agent"),
            "home_business_id": None}],
    }
    agents = {}
    for business_id, rowset in rows.items():
        for row in rowset:
            agents[(business_id, row["agent_id"])] = (SimpleNamespace(agent_id=row["agent_id"], data={"name": row["name"]}), row)
    host, first, second = Broker("host"), Broker("one"), Broker("two")
    prepared = []

    def prepare(*args, **kwargs):
        prepared.append(kwargs)

    bundles = {
        "one": TerminalExecution(first, "http://one-service/mcp", "/one/python", lambda: {"claude-code": "one"},
            lambda profile, business: {"BUSINESS": business["slug"], "AGENT": profile.agent_id}, "one-generation",
            lambda path: path == "/one/python"),
        "two": TerminalExecution(second, "http://two-service/mcp", "/two/python", lambda: {"claude-code": "two"},
            lambda profile, business: {"BUSINESS": business["slug"], "AGENT": profile.agent_id}, "two-generation"),
    }
    terminal = ProbeTerminal(Runtime(root, rows), host, agents, service_url="http://host/mcp",
        preparation=prepare, version_provider=lambda: {"claude-code": "host"},
        environment=lambda business: {"HOST": business["slug"]},
        global_environment=lambda: {"GLOBAL": "yes"}, execution_provider=lambda business: bundles.get(business["id"]))
    return terminal, (business_one, business_two), (host, first, second), prepared, bundles


def context(business_id):
    return SimpleNamespace(business_id=business_id)


@pytest.mark.parametrize("target", ["business", "global", "workspace"])
def test_recovery_excludes_new_terminal_writers_through_session_creation(tmp_path, target):
    from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
    terminal, (one, _), (host, first, _), _, _ = make_terminal(tmp_path)
    lock = InstallationRecoveryLock(terminal.runtime.root)
    terminal.runtime.application.writer_admission = RecoveryAdmission(installation_lock=lock)
    broker = first if target == "business" else host
    original, checked = broker.ensure_session, []
    def ensure(*args, **kwargs):
        with pytest.raises(TimeoutError):
            with lock.exclusive(timeout=0):
                pass
        checked.append(True)
        return original(*args, **kwargs)
    broker.ensure_session = ensure
    terminal.workspace.mkdir(parents=True, exist_ok=True)
    if target == "business":
        open_terminal = lambda: terminal.open(context("one"), one, AGENT_ONE)
    elif target == "global":
        open_terminal = lambda: terminal.open(context(None), None, AGENT_GLOBAL)
    else:
        open_terminal = lambda: terminal.open_workspace(context(None))
    open_terminal()
    assert checked == [True]
    with lock.exclusive(timeout=0):
        with pytest.raises(DomainError) as error:
            open_terminal()
        assert error.value.code == "maintenance"
    assert checked == [True]


def test_businesses_use_distinct_brokers_and_preparation_targets(tmp_path):
    terminal, (one, two), (host, first, second), prepared, _ = make_terminal(tmp_path)
    mirror_one, _, row_one = terminal.open(context("one"), one, AGENT_ONE)
    mirror_two, _, _ = terminal.open(context("two"), two, AGENT_TWO)

    assert mirror_one is first.mirrors[next(iter(first.mirrors))]
    assert mirror_two is second.mirrors[next(iter(second.mirrors))]
    assert not host.ensure_calls
    assert prepared[0]["endpoint"] == "http://one-service/mcp"
    assert prepared[0]["interpreter"] == "/one/python"
    assert prepared[0]["interpreter_validator"]("/one/python")
    assert first.ensure_calls[-1][-1]["env"] == {"BUSINESS": "one", "AGENT": AGENT_ONE,
        "QORQUT_REGISTRATION": str(terminal.runtime.root / "one/hq/agents/one-agent/.bazarlyk/registration.json")}
    assert terminal.binding(one, row_one, first)[1].startswith("one:one:")

    assert terminal.listing(context("one"), one) == [{"agent": "one-agent", "agent_id": AGENT_ONE, "done": False}]
    assert terminal.listing(context("two"), two) == [{"agent": "two-agent", "agent_id": AGENT_TWO, "done": False}]
    assert first.ensure_calls and second.ensure_calls
    assert not host.attach_calls


def test_global_terminal_stays_on_host_and_pending_business_fails_closed(tmp_path):
    terminal, (one, _), (host, first, _), _, _ = make_terminal(tmp_path)
    terminal.open(context(None), None, AGENT_GLOBAL)
    assert host.ensure_calls and not first.ensure_calls

    terminal.broker = None
    with pytest.raises(DomainError, match="backend is unavailable"):
        asyncio.run(terminal.route(Request("POST"), context(None), None,
            ["terminal", AGENT_GLOBAL, "stop"]))

    pending = {"id": "pending", "slug": "pending", "hq_path": str(tmp_path / "pending/hq"), "stage": "sandbox_pending"}
    terminal.execution_provider = lambda business: (_ for _ in ()).throw(
        DomainError("operation_unavailable", "Business terminal backend is pending"))
    pending_agent = AGENT_PENDING
    with pytest.raises(DomainError, match="pending"):
        terminal.open(context("pending"), pending, pending_agent)
    with pytest.raises(DomainError, match="pending"):
        terminal.listing(context("pending"), pending)


def test_actions_and_stream_reconnect_use_selected_business_broker(tmp_path):
    terminal, (one, two), (host, first, second), _, _ = make_terminal(tmp_path)
    terminal.open(context("one"), one, AGENT_ONE)
    terminal.open(context("two"), two, AGENT_TWO)

    asyncio.run(terminal.route(Request("POST", {"data": "hello"}), context("one"), one,
        ["terminal", AGENT_ONE, "input"]))
    asyncio.run(terminal.route(Request("POST", {"cols": 90, "rows": 24}), context("one"), one,
        ["terminal", AGENT_ONE, "resize"]))
    asyncio.run(terminal.route(Request("POST"), context("two"), two,
        ["terminal", AGENT_TWO, "stop"]))
    assert first.mirrors[next(iter(first.mirrors))].inputs == [b"hello"]
    assert first.mirrors[next(iter(first.mirrors))].resizes == [(24, 90)]
    assert second.kill_calls and second.stop_calls
    assert not host.kill_calls and not host.stop_calls

    key = next(iter(first.mirrors))
    first.mirrors[key].done = True
    request = Request("GET")
    request.is_disconnected = _disconnect_after_reconnect
    stream = asyncio.run(terminal.route(request, context("one"), one,
        ["terminal", AGENT_ONE, "stream"]))
    asyncio.run(_consume(stream.body_iterator))
    assert any(call[0] == key for call in first.attach_calls if isinstance(call, tuple))


async def _disconnect_after_reconnect():
    if not hasattr(_disconnect_after_reconnect, "calls"):
        _disconnect_after_reconnect.calls = 0
    _disconnect_after_reconnect.calls += 1
    return _disconnect_after_reconnect.calls > 1


async def _consume(iterator):
    async for _ in iterator:
        pass
    _disconnect_after_reconnect.calls = 0


def test_stream_ends_when_execution_generation_changes(tmp_path, monkeypatch):
    terminal, (one, _), (_, first, _), _, bundles = make_terminal(tmp_path)
    current = [bundles["one"], TerminalExecution(Broker("replaced"), "http://replaced/mcp", "/replaced/python",
        lambda: {"claude-code": "replaced"}, bundles["one"].environment_resolver, "replaced-generation")]
    terminal.execution_provider = lambda business: current.pop(0)
    clock = iter((0, 6, 6))
    monkeypatch.setattr(terminal_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    request = Request("GET")
    disconnected = iter((False, True))
    request.is_disconnected = lambda: _next_async(disconnected)
    stream = asyncio.run(terminal.route(request, context("one"), one,
        ["terminal", AGENT_ONE, "stream"]))
    events = asyncio.run(_collect(stream.body_iterator))
    assert b"event: done" in b"".join(events)
    assert len([call for call in first.attach_calls if isinstance(call, tuple)]) == 1


def test_stream_accepts_fresh_callables_for_unchanged_execution_identity(tmp_path, monkeypatch):
    terminal, (one, _), (_, first, _), _, _ = make_terminal(tmp_path)
    terminal.execution_provider = lambda business: TerminalExecution(first, "http://one-service/mcp", "/one/python",
        lambda: {"claude-code": "one"}, lambda profile, business: {"BUSINESS": "one"}, "one-generation")
    clock = iter((0, 6, 6))
    monkeypatch.setattr(terminal_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    request = Request("GET")
    disconnected = iter((False, True))
    request.is_disconnected = lambda: _next_async(disconnected)
    stream = asyncio.run(terminal.route(request, context("one"), one,
        ["terminal", AGENT_ONE, "stream"]))
    events = asyncio.run(_collect(stream.body_iterator))
    assert events == []
    assert len([call for call in first.attach_calls if isinstance(call, tuple)]) == 1


async def _next_async(values):
    return next(values)


async def _collect(iterator):
    return [event async for event in iterator]


def test_stream_attach_skips_preparation_only_for_existing_session(tmp_path):
    terminal, (one, _), (_, first, _), prepared, _ = make_terminal(tmp_path)
    terminal.open(context("one"), one, AGENT_ONE)
    initial = len(prepared)
    asyncio.run(terminal.route(Request("GET"), context("one"), one,
        ["terminal", AGENT_ONE, "stream"]))
    assert len(prepared) == initial
    assert len(first.ensure_calls) == 1
    first.sessions.clear()
    asyncio.run(terminal.route(Request("GET"), context("one"), one,
        ["terminal", AGENT_ONE, "stream"]))
    assert len(prepared) == initial + 1
    assert len(first.ensure_calls) == 2


def test_prepared_agent_shell_only_applies_to_agent_terminal(tmp_path):
    terminal, _, (host, _, _), _, _ = make_terminal(tmp_path)
    rc = terminal.runtime.root / 'agents/global-agent/.bazarlyk/agent-shell.bash'
    rc.parent.mkdir(parents=True)
    rc.write_text('# prepared shell\n')
    terminal.open(context(None), None, AGENT_GLOBAL)
    assert host.ensure_calls[-1][1] == ['/bin/bash', '--rcfile', str(rc), '-i']
    terminal.workspace.mkdir(parents=True, exist_ok=True)
    terminal.open_workspace(context(None))
    assert host.ensure_calls[-1][1] == ['/bin/bash', '-i']
