"""Run isolated installed-wheel prompt freshness check with a disposable instance.

Usage: PREPARED/venv/bin/python -I -B installed_prompt_freshness.py
       --prepared-root PREPARED --scratch NEW_EMPTY_DIRECTORY
Only the native attestation, transport and process-presence boundaries are simulated.
No network, service, or native model process starts.
"""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from unittest.mock import patch

from bazarlyk.agentic.support import digest
from bazarlyk.bootstrap import claude_session, handoff, preparation, supervisor
from bazarlyk.bootstrap.client import load_registration, write_json_private
from bazarlyk.bootstrap.initialize import initialize, load_prepared
from bazarlyk.extensions import installed_selection
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.service.runtime import build_runtime
from qorqut_operations.contracts import Principal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepared-root', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    prepared, scratch = args.prepared_root.resolve(), args.scratch.absolute()
    if not sys.flags.isolated or Path(sys.prefix).resolve() != prepared / 'venv':
        raise ValueError('Run the prepared interpreter with -I')
    scratch.mkdir(parents=True, exist_ok=False)
    root = scratch / 'instance'
    receipt = initialize(prepared, root, owner_name='Synthetic Owner')
    _, _, components = load_prepared(prepared)
    paths = InstancePaths(root, components)
    runtime = build_runtime(prepared / 'src', paths=paths, extensions=installed_selection({'extensions': []}))
    actor = Principal(receipt['agents']['ordubashy'], 'agent')
    config = root / 'config/installation.json'
    write_json_private(config, {'format': 1, 'root': str(root)})
    receipt_results = []
    class Transport:
        def __init__(self, registration, **kwargs): self.timeout = 30
        def post(self, route, body):
            if route.endswith('/receipt'):
                result = runtime.resolver.record_delivery(actor, **body)
                receipt_results.append(result)
                return result
            return runtime.resolver.bootstrap(actor, **body)
    outcomes = []
    from qorqut_agentic import catalog_api
    real_load = catalog_api.load_catalog
    loads = []
    def count_load(source):
        loads.append(1)
        return real_load(source)
    try:
        with runtime.owned_preparation(), patch.dict(os.environ, {'QORQUT_INSTALLATION_CONFIG': str(config)}), \
                patch('bazarlyk.installation.helper_launcher', return_value=None), \
                patch.object(preparation, 'checkpoint_protocols', return_value={'claude-code': 0, 'codex': 0}), \
                patch('bazarlyk.bootstrap.diagnostics.prepared_helpers', return_value={'ready': True, 'reason': 'synthetic'}), \
                patch.object(handoff, 'running', return_value=True), \
                patch.object(claude_session, 'snapshot_transcript', return_value=None), \
                patch.object(supervisor, 'BootstrapClient', Transport), \
                patch.object(catalog_api, 'load_catalog', side_effect=count_load):
            ready = preparation.prepare_agent(runtime.resolver, actor, endpoint='http://testserver',
                versions={'claude-code': '2.1.263 (Claude Code)', 'codex': 'codex-cli 0.153.4'},
                interpreter=sys.executable, interpreter_validator=lambda path: Path(path).is_file())
            workspace = Path(ready['workspace'])
            path = workspace / '.bazarlyk/registration.json'
            registration = load_registration(path)
            registration_before = path.read_bytes()
            credential_path = workspace / registration['credential_file']
            credential_before = credential_path.read_bytes()
            with runtime.database.read() as uow:
                profile_before = tuple(uow.connection.execute(
                    'SELECT * FROM agent_profiles WHERE agent_id=?', (actor.id,)).fetchone())
            layer = runtime.agentic_layer
            attribution = {'actor_id': actor.id, 'actor_kind': 'agent', 'reason': 'Synthetic owner instruction'}
            for index, kind in enumerate(('codex', 'claude-code')):
                session = str(uuid.uuid4())
                native = {'registration_id': registration['registration_id'], 'session_id': session,
                    'owner': '123:456', 'runtime': kind, 'event': 'prompt', 'registration_path': str(path)}
                def attest(*unused):
                    write_json_private(workspace / '.bazarlyk/native-binding.json', native)
                    return dict(native)
                def invoke():
                    output = io.StringIO()
                    claude_session.run({'hook_event_name': 'UserPromptSubmit', 'session_id': session,
                        'cwd': str(workspace), 'prompt': 'Continue'}, path, stream=output,
                        diagnostics=io.StringIO(), runtime=kind, client_factory=Transport)
                    return json.loads(output.getvalue())
                with patch('bazarlyk.bootstrap.native_binding.attest', side_effect=attest):
                    attest()
                    old = runtime.resolver.bootstrap(actor, registration_id=registration['registration_id'],
                        session_id=session, event='prompt', runtime=kind)
                    assert old['ready'], old
                    body = {'registration_id': registration['registration_id'], 'session_id': session,
                        'event': 'prompt', 'last_effective_config_id': None, 'runtime': kind}
                    handoff.offer(root, path, registration, native, body, revision=old['agentic_catalog_revision'])
                    previous = handoff.read(handoff.pending_path(root, native))
                    assert handoff.publish(root, registration, previous, response=old)
                    pref = 'user-preferences/SKILL.md'
                    original = (layer.root / pref).read_bytes()
                    marker = f'Synthetic healthy preference {index}.'
                    layer.authoring.apply(attribution, [{'op': 'edit', 'path': pref,
                        'expected_sha256': digest(original), 'content': original.decode() + '\n' + marker + '\n'}])
                    if index == 0:
                        layer.authoring.apply(attribution, [
                            {'op': 'create', 'path': 'plugins/fixture-plugin/agentic.yaml', 'content':
                             'schema_version: 1\nid: fixture-plugin\nname: Fixture\ndescription: Synthetic\nentries:\n  - id: guide\n    kind: instruction\n    path: guide.md\n    description: Synthetic\n    when_to_use: Always\n'},
                            {'op': 'create', 'path': 'plugins/fixture-plugin/guide.md', 'content': 'Synthetic plugin.\n'}])
                    else:
                        plugin = 'plugins/fixture-plugin/guide.md'
                        layer.authoring.apply(attribution, [{'op': 'edit', 'path': plugin,
                            'expected_sha256': digest((layer.root / plugin).read_bytes()), 'content': 'Updated synthetic plugin.\n'}])
                    current_revision = runtime.agentic_view.catalog_revision
                    worker = supervisor.Supervisor({'root': str(root)}, alive=lambda _: True)
                    timings = {}
                    stop = threading.Event()
                    def work():
                        while not stop.is_set():
                            item = handoff.read(handoff.pending_path(root, native))
                            if item.get('request_id') != previous['request_id'] and item.get('catalog_revision') == current_revision:
                                timings['started'] = time.monotonic()
                                try: worker.recover(item)
                                except Exception as exc: timings['error'] = repr(exc)
                                timings['finished'] = time.monotonic()
                                return
                            time.sleep(.005)
                    thread = threading.Thread(target=work)
                    load_before = len(loads)
                    thread.start()
                    started = time.monotonic()
                    first = invoke()
                    ended = time.monotonic()
                    thread.join(timeout=30)
                    stop.set()
                    thread.join()
                    worker.pool.shutdown(wait=True)
                    text = json.dumps(first)
                    stale = json.dumps(old['mandatory_context']['rendered'])[1:-1] in text
                    delivered = marker in text and not stale
                    assert not stale, 'A stale warm response was delivered'
                    result = {'runtime': kind, 'budget_seconds': handoff.HOOK_WAIT,
                              'hook_seconds': round(ended-started, 3), 'delivered_current': delivered,
                              'first_pending': 'bootstrap_pending' in text, 'stale_delivered': stale,
                              'supervisor_start_seconds': round(timings['started']-started, 3) if 'started' in timings else None,
                              'supervisor_seconds': round(timings['finished']-timings['started'], 3) if 'finished' in timings else None,
                              'error': timings.get('error'), 'recovery_catalog_loads': len(loads)-load_before}
                    if not delivered:
                        retry_started = time.monotonic()
                        retry = invoke()
                        result['retry_seconds'] = round(time.monotonic() - retry_started, 3)
                        result['next_prompt_current'] = marker in json.dumps(retry)
                        result['retry_pending'] = 'bootstrap_pending' in json.dumps(retry)
                    result['revision_match'] = (handoff.read(handoff.result_path(root, native))
                        .get('response', {}).get('agentic_catalog_revision') == current_revision)
                    for queued in root.joinpath('state/agent-followups').glob('*-receipt.json'):
                        item = handoff.read(queued)
                        if item.get('session_id') == session:
                            if worker.followup(item):
                                queued.unlink()
                    result['receipt_recorded'] = bool(receipt_results and receipt_results[-1].get('recorded'))
                    with runtime.database.read() as uow:
                        result['delivery_rows'] = uow.connection.execute(
                            'SELECT COUNT(*) FROM session_deliveries WHERE registration_id=? AND session_id=?',
                            (registration['registration_id'], session)).fetchone()[0]
                    unchanged_started = time.monotonic()
                    unchanged = invoke()
                    result['unchanged_seconds'] = round(time.monotonic()-unchanged_started, 3)
                    result['unchanged_empty'] = unchanged == {}
                    result['unchanged_pending'] = 'bootstrap_pending' in json.dumps(unchanged)
                    result['unchanged_repeated_body'] = marker in json.dumps(unchanged)
                    with runtime.database.read() as uow:
                        result['profile_preserved'] = tuple(uow.connection.execute(
                            'SELECT * FROM agent_profiles WHERE agent_id=?', (actor.id,)).fetchone()) == profile_before
                    result['registration_preserved'] = path.read_bytes() == registration_before
                    result['credential_preserved'] = credential_path.read_bytes() == credential_before
                    result['session_preserved'] = handoff.observation(registration).get('session_id') == session
                    outcomes.append(result)
                    print(json.dumps(result), flush=True)
            (scratch / 'results.json').write_text(json.dumps(outcomes, indent=2) + '\n')
            assert all(item['delivered_current'] and item.get('revision_match') and item.get('receipt_recorded') and item.get('delivery_rows') == 1 and item.get('unchanged_empty') and item.get('profile_preserved') and item.get('registration_preserved') and item.get('credential_preserved') and item.get('session_preserved') for item in outcomes), outcomes
    finally:
        runtime.domains.shared_context.close()


if __name__ == '__main__':
    main()
