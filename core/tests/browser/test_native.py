"""Browser entry installation preserves owned core/hooks and native preferences."""

import json
import time
import tomllib
import unittest

from runtime.test_native_setup import fixture, setup_state
from .peer import Peer
from .test_transport import wait_for


class NativeSetup(unittest.TestCase):
    def test_attachment_invalidates_setup_cache_without_native_launch_or_preference_changes(self):
        f = fixture()
        self.addCleanup(f.close)
        f.start()
        setup_state(f, 'ready')
        host = f.post('agents_get', {'agent_id': f.agent_id}).json()['data']
        epoch = f.post('config_get').json()['data']['recovery_epoch']
        peer = Peer(f.agent_id, epoch)
        self.addCleanup(peer.close)
        codex = f.workspace.parent / 'runtime/home/.codex/config.toml'
        original = codex.read_bytes()
        codex.write_bytes(b'model = "authored-model"\nmodel_reasoning_effort = "medium"\n' + original +
            b'\n[projects."/authored"]\ntrust_level = "trusted"\n')
        mcp = f.workspace / '.mcp.json'
        value = json.loads(mcp.read_bytes())
        value['mcpServers']['authored'] = {'command': 'custom-provider'}
        mcp.write_text(json.dumps(value))
        core = value['mcpServers']['qorqut-core']
        protected = [f.workspace / 'persona.md', f.workspace / '.claude/settings.local.json',
            f.workspace.parent / 'runtime/home/.codex/hooks.json', *f.workspace.glob('.qorqut/manual-hooks/*.json')]
        saved = {path: path.read_bytes() for path in protected}
        instructions = {name: (f.workspace / name).read_bytes() for name in ('AGENTS.md', 'CLAUDE.md')}
        attached = f.post('browsers_attach', {'agent_id': f.agent_id, 'service_url': peer.url, 'idempotency_key': 'native-attach'}).json()
        self.assertTrue(attached['ok'], attached)
        wait_for(lambda: (f.workspace / '.qorqut/manual-browser/preparation.json').exists())
        expected = {'X-Qorqut-Actor': host['actor_id']}
        parsed = tomllib.loads(codex.read_text())
        self.assertEqual((parsed['model'], parsed['model_reasoning_effort']), ('authored-model', 'medium'))
        self.assertEqual(parsed['projects']['/authored']['trust_level'], 'trusted')
        self.assertEqual(parsed['mcp_servers']['qorqut-browser'], {'url': peer.url + '/mcp', 'required': False, 'http_headers': expected})
        servers = json.loads(mcp.read_bytes())['mcpServers']
        self.assertEqual(servers['qorqut-core'], core)
        self.assertEqual(servers['authored'], {'command': 'custom-provider'})
        self.assertEqual(servers['qorqut-browser'], {'type': 'http', 'url': peer.url + '/mcp', 'headers': expected})
        self.assertEqual(saved, {p: p.read_bytes() for p in saved})
        for name, before in instructions.items():
            body = (f.workspace / name).read_bytes()
            self.assertTrue(body.startswith(before))
            self.assertEqual(body.count(b'QORQUT CORE INSTRUCTIONS BEGIN'), 1)
            self.assertEqual(body.count(b'QORQUT BROWSER INSTRUCTIONS BEGIN'), 1)
            self.assertIn(b'completion or cancellation notification', body)
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (codex, mcp, *protected)}
        time.sleep(1.1)
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in snapshot})
        self.assertFalse((f.root / 'unexpected-native-launch').exists())
        f.stop()
        f.start()
        setup_state(f, 'ready')
        self.assertEqual({p: value[0] for p, value in snapshot.items()}, {p: p.read_bytes() for p in snapshot})
        self.assertFalse((f.root / 'unexpected-native-launch').exists())
