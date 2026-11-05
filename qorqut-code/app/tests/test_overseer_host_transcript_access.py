import json

import pytest
from qorqut_operations.contracts import DomainError, Principal

from bazarlyk.overseer.access import EvidenceAccess
from bazarlyk.overseer.coverage import AnalysisCoverage
from bazarlyk.overseer.evidence import EvidenceProvider
from host_foundation_support import create_registered_global, grant_capabilities, owner_global_context
from native_chat_support import Native
from test_agentic_access import registration


def test_real_admission_accepts_p02_operations_transcript_entry_shape(tmp_path):
    native = Native(tmp_path, 'claude')
    native.publish()
    native.pin()
    transcript_text = 'Synthetic agent result. ' + ('evidence ' * 100)
    native.write(native.claude_assistant(transcript_text))
    native.pass_(2)
    runtime = native.runtime
    overseer = create_registered_global(native.instance, 'learning-applier')
    grant_capabilities(native.instance, overseer, 'read_agent_experience')
    # Exercise the shipped role, without a fixture-only read-policy override.
    registration_id = registration(native.instance, overseer)
    ctx = runtime.resolver.runtime_context(Principal(overseer, 'agent'), session_registration_id=registration_id)
    archive = runtime.paths.overseer_archive
    archive.mkdir(mode=0o700, parents=True)
    gate = EvidenceAccess(runtime, ctx)
    provider = EvidenceProvider(runtime, ctx, archive, gate, overseer_ids={overseer}, coverage=AnalysisCoverage(runtime.agentic_layer))
    selected = {'mode': 'explicit', 'namespaces': [{'kind': 'global', 'business_id': None}], 'agent_ids': [native.agent]}
    result = provider.list_changes(selected)
    entries = [item for item in result['items'] if item['kind'] == 'transcript_entry']
    entry = entries[0]
    assert entry['provider'] == 'operations'
    assert provider.get_source(entry)['content']['text'] == transcript_text
    episode_ref = {'id': entry['episode_id']}
    first = provider.get_episode(episode_ref, result['frontier'], max_chars=61)
    assert first['next_cursor'] and first['records'][0]['source_ref']['kind'] == 'transcript_entry'
    cursor, pages, offsets, fragments = None, 0, {}, []
    while True:
        page = provider.get_episode(episode_ref, result['frontier'], cursor=cursor, max_chars=73)
        pages += 1
        for record in page['records']:
            assert record['offset'] == offsets.get(record['source_ref']['record_id'], 0)
            offsets[record['source_ref']['record_id']] = offsets.get(record['source_ref']['record_id'], 0) + len(record['content_fragment'])
            fragments.append(record['content_fragment'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert pages > 1 and len(entries) == 1
    expected = json.dumps(provider.get_source(entry)['content'], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    assert ''.join(fragments) == expected

    old_frontier_first = provider.get_episode(episode_ref, result['frontier'], max_chars=20)
    assert old_frontier_first['next_cursor']
    native.write(native.claude_assistant('Later independent result.'))
    native.pass_(2)
    with runtime.database.transaction() as uow:
        latest_receipt = uow.connection.execute('SELECT receipt_id FROM native_captures WHERE agent_id=? AND provider=? AND conversation_id=?',
            (entry['locator']['agent_id'], entry['locator']['provider'], entry['locator']['conversation_id'])).fetchone()[0]
    assert latest_receipt != entry['locator']['receipt_id']
    assert provider.get_source(entry)['content']['text'] == transcript_text
    resumed_old_frontier = provider.get_episode(episode_ref, result['frontier'],
        cursor=old_frontier_first['next_cursor'])
    assert resumed_old_frontier['records'][0]['offset'] == 20
    assert {ref['record_id'] for ref in resumed_old_frontier['source_refs']} == {entry['record_id']}
    grown = provider.list_changes(selected)
    grown_entries = [item for item in grown['items'] if item['kind'] == 'transcript_entry']
    grown_ids = [item['record_id'] for item in grown_entries]
    assert grown_ids.count(entry['record_id']) == 1
    assert len(grown_ids) == len(set(grown_ids)) and any(identity != entry['record_id'] for identity in grown_ids)
    # Default read access must never override an explicit owner denial.
    with runtime.database.read() as uow:
        revision = uow.connection.execute('SELECT revision FROM agent_profiles WHERE agent_id=?', (overseer,)).fetchone()[0]
    changed = runtime.application.invoke(owner_global_context(native.instance), 'agents_update',
        {'agent_id': overseer, 'changes': {'tool_policy': {'overrides': {'agents_get': 'deny'}}}},
        {'expected_revision': revision, 'idempotency_key': 'synthetic-transcript-denied'})
    assert changed.status == 'ok', changed.error
    with pytest.raises(DomainError) as denied:
        provider.get_source(entry)
    assert denied.value.code == 'policy_denied'
