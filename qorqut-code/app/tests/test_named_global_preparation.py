"""Synthetic relocation of a fixed global, keeping its registration and identity."""
import json
import sqlite3

from host_foundation_support import build_instance, create_registered_global, register_sources
from bazarlyk.bootstrap.preparation import prepare_agent
from qorqut_operations.contracts import Principal


def test_named_global_reprepares_existing_registration(tmp_path):
    instance, _ = build_instance(tmp_path)
    register_sources(instance, instance.business, 'first')
    agent = create_registered_global(instance, "learning-applier")             # registered as ``learning-applier``
    versions = {'codex': 'codex-cli 0.156.1', 'claude-code': '2.1.263 (Claude Code)'}
    first = prepare_agent(instance.runtime.resolver, Principal(agent, 'agent'),
                          endpoint='http://127.0.0.1:9988', versions=versions)
    old = instance.paths.agent_workspace(agent)
    with sqlite3.connect(instance.paths.database) as connection:
        name = connection.execute('SELECT name FROM agent_profiles WHERE agent_id=?', (agent,)).fetchone()[0]
        registration = connection.execute('SELECT id,credential_id FROM machine_registrations WHERE workspace=?', (str(old),)).fetchone()
    assert name in {'ordubashy', 'learning-applier'}
    (old / 'notes' / 'personal.md').write_text('Keep my authored note.\n')
    new = old.parent / name
    old.rename(new)
    config = instance.paths.instance_root / 'config'
    config.mkdir(exist_ok=True)
    (config / 'global-agent-workspaces.json').write_text(json.dumps({'version': 1, 'agents': {agent: name}}))
    with instance.database.transaction() as uow:
        connection = uow.connection
        connection.execute('UPDATE agent_profiles SET workspace=? WHERE agent_id=?', (str(new), agent))
        connection.execute('UPDATE machine_registrations SET workspace=? WHERE workspace=?', (str(new), str(old)))
    result = prepare_agent(instance.runtime.resolver, Principal(agent, 'agent'),
                           endpoint='http://127.0.0.1:9988', versions=versions)
    assert result['workspace'] == str(new)
    assert (new / 'notes' / 'personal.md').read_text() == 'Keep my authored note.\n'
    with sqlite3.connect(instance.paths.database) as connection:
        assert connection.execute('SELECT id,credential_id FROM machine_registrations WHERE workspace=?', (str(new),)).fetchone() == registration
    generated = json.loads((new / '.bazarlyk/registration.json').read_text())
    assert generated['workspace'] == str(new) and generated['agent_id'] == agent
