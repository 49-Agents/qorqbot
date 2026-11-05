"""The default native binding adapter reaches the installed TerminalService of the assembled service.

Earlier legacy/strict binding tests installed their own native adapter, so none noticed that production composition never gave
the runtime its terminal service. This assembles the real service (``assemble``/``create_app``) with the default
``NativeBindingAdapter`` over a disposable migrated database, a real pty pane running a native-named interactive client and
the real probes. Only the tmux registry is a fixture: it records the exact pane lookup and answers with that pane. No model,
live database, service or provider is used."""
import pytest
from starlette.testclient import TestClient

from bazarlyk.bootstrap.native_binding import NativeBindingAdapter
from qorqut_operations.contracts import DomainError
from bazarlyk.release_worker.lifetime import atomic_json
from bazarlyk.service.main import assemble
from test_agent_configuration import REPO, PATHS
from test_legacy_bridge_compatibility import legacy  # noqa: F401  (the fixture)
from test_legacy_native_probe import Fixture as Panes
from test_service_transports import OWNER_TOKEN


class Registry:
    """The tmux registry: one pane per registered terminal, and a record of what was asked."""

    TMUX = '/usr/bin/tmux'

    def __init__(self):
        self.pane, self.asked = '', []

    @staticmethod
    def session_name(slug, agent):
        return f'{slug}__{agent}'

    def tmux(self, *arguments):
        self.asked.append(arguments)
        return 0, f'{self.pane}\n'


def refused(*args, **kwargs):
    raise AssertionError('Native binding verification must not use a model')


@pytest.fixture
def assembled(legacy, tmp_path):  # noqa: F811
    legacy.migrate()
    panes = Panes(tmp_path / 'native')
    registry = Registry()
    app = assemble(legacy.service.fixture.root, REPO, paths=PATHS, origins=['http://testserver'], endpoint='http://testserver/mcp',
        bootstrap_token=OWNER_TOKEN, database=legacy.service.fixture.db, signing_key=b'fixed-key' * 8, broker=registry,
        model_client_factory=refused, nightly_maintenance=None)
    pane, native, started = panes.pane()
    registry.pane = str(pane)
    observation = {'agent_id': legacy.agent.id, 'registration_id': legacy.registration, 'session_id': legacy.consumer,
        'runtime': 'claude-code', 'workspace': legacy.workspace, 'pid': native, 'started': started,
        'owner': f'{native}:{started}', 'event': 'prompt'}
    atomic_json(legacy.workspace + '/.bazarlyk/native-binding.json', observation)
    yield legacy, app, registry, observation
    panes.close()


def request(path, legacy, app, registry):
    """What the existing owner's supervisor asks, or what a startup bootstrap verifies, through the assembled service."""
    if path == 'legacy':
        reply = legacy.route(TestClient(app), '/api/agent/inbox/legacy', {'consumer_id': legacy.consumer})
        if reply.status_code != 200:
            raise DomainError(reply.json()['error']['code'], reply.text)
        data = reply.json()['data']
        return data['owner'], data['runtime']
    value, state = NativeBindingAdapter(app.state.runtime).verify(legacy.agent.id, legacy.registration, legacy.consumer)
    return value['owner'], value['runtime']


@pytest.mark.parametrize('path', ['legacy', 'normal'])
def test_the_default_adapter_verifies_an_existing_session_through_the_installed_terminal_service(assembled, path):
    legacy, app, registry, observation = assembled
    runtime = app.state.runtime
    terminals = app.state.http_api.terminals
    # One terminal service backs the HTTP routes, terminal control and the binding adapter; no test adapter is installed.
    assert terminals is not None and runtime.terminals is terminals
    assert runtime.terminal_control.backend.terminals is terminals
    assert getattr(runtime, 'native_binding_adapter', None) is None
    assert request(path, legacy, app, registry) == (observation['owner'], 'claude-code')
    with runtime.database.read() as uow:
        profile = dict(uow.connection.execute('SELECT * FROM agent_profiles WHERE agent_id=?', (legacy.agent.id,)).fetchone())
        business = uow.repository('businesses').get(profile['home_business_id'])
    # The exact terminal the installed service binds to this agent was looked up through its selected broker.
    selected = terminals.execution(business)
    assert selected.broker is registry
    _, session, _ = terminals.binding(business, profile, selected.broker)
    assert registry.asked == [('list-panes', '-t', '=' + session, '-F', '#{pane_pid}')]


@pytest.mark.parametrize('path', ['legacy', 'normal'])
def test_an_unverified_owner_is_still_refused_through_the_assembled_service(assembled, path):
    legacy, app, registry, observation = assembled
    # The recorded process start is another process's (a reused PID): the live process does not match the observation.
    atomic_json(legacy.workspace + '/.bazarlyk/native-binding.json', {**observation, 'started': str(int(observation['started']) + 1),
        'owner': f"{observation['pid']}:{int(observation['started']) + 1}"})
    with pytest.raises(DomainError) as error:
        request(path, legacy, app, registry)
    assert error.value.code == 'inbox_binding_ambiguous'
    # A terminal with no registered pane never verifies either.
    atomic_json(legacy.workspace + '/.bazarlyk/native-binding.json', observation)
    registry.pane = ''
    with pytest.raises(DomainError) as error:
        request(path, legacy, app, registry)
    assert error.value.code == 'inbox_binding_ambiguous'
