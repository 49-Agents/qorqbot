"""Approved defaults validated against a disposable real host registry."""
from pathlib import Path

import pytest

from bazarlyk.catalog.packages import load_package
from host_foundation_support import build_instance


def test_real_default_role_policies_and_canonical_skills(tmp_path, monkeypatch):
    from bazarlyk.agentic import installation
    instance, _ = build_instance(tmp_path, businesses=())
    defaults = Path(__file__).resolve().parents[3] / 'qorqut-agentic/src/qorqut_agentic/resources/defaults'
    from bazarlyk.catalog.sources import load_sources
    sources = load_sources(defaults.parents[4], config_path=Path(__file__).resolve().parents[1] / 'package-sources.json')
    versions = {'global-worker': '1.7.0', 'learning-applier': '1.17.0', 'ordubashy': '1.73.0'}
    try:
        # T02's read-only revision observer reconstructs paths and validates
        # the private binding before opening the module.
        import json
        from bazarlyk.instance_paths import InstancePaths
        from bazarlyk.telegram.private_files import read_private_file
        record = json.loads(read_private_file(instance.paths.instance_root / 'config' / installation.RECORD,
                                              max_bytes=installation.MAX_RECORD))
        installation._validate_binding(InstancePaths(instance.paths.instance_root, {}), record)
        for role, version in versions.items():
            kind, source, manifest = sources[role]
            assert (kind, source, manifest) == ('role', defaults, f'roles/imported-roles/native/roles/{role}/role.yaml')
            package = load_package(source, manifest, kind, instance.runtime.registry)
            assert package.version == version
            selection = instance.runtime.agentic_view.selection({'role': {'id': role}})
            for skill in package.skills:
                files = instance.runtime.agentic_view.skill_files('role', package, skill['id'], selection=selection)
                assert files['SKILL.md'] == (defaults / skill['source_path']).read_bytes()
            for operation in package.manifest['default_tool_policy']:
                assert instance.runtime.registry.metadata(operation).name == operation
        from qorqut_operations.contracts import DomainError
        from bazarlyk.agents.preferences import load_preferences
        preference = instance.runtime.agentic_layer.root / 'user-preferences/SKILL.md'
        original = preference.read_text()
        with pytest.raises(DomainError) as conflict:
            with instance.runtime.agentic_view.read_snapshot():
                assert load_preferences(instance.runtime.agentic_view).text == original
                preference.write_text(original + '\nSynthetic changed preference.\n')
        assert conflict.value.code == 'agentic_revision_conflict' and conflict.value.retryable
        with instance.runtime.agentic_view.read_snapshot():
            assert 'Synthetic changed preference.' in load_preferences(instance.runtime.agentic_view).text
        import time
        from bazarlyk.bootstrap.catalog_revision import open_observer
        with open_observer(instance.paths.instance_root, time.monotonic() + 15) as observer:
            first_pid = observer.process.pid
            old = observer.observe()
            preference.write_text(original + '\nA newer preference.\n')
            new = observer.observe()
            assert old != new
        assert observer.process.poll() is not None
        binding_path = instance.paths.instance_root / 'config' / installation.RECORD
        binding = binding_path.read_bytes()
        try:
            with open_observer(instance.paths.instance_root, time.monotonic() + 15) as second:
                assert second.process.pid != first_pid
                assert second.observe() == new
                binding_path.write_bytes(binding.replace(b'"instance_id":', b'"wrong_id":', 1))
                with pytest.raises(DomainError) as error:
                    second.observe()
                assert error.value.code == 'bootstrap_pending'
            assert second.process.poll() is not None
        finally:
            binding_path.write_bytes(binding)
    finally:
        instance.runtime.domains.shared_context.close()
        instance.runtime.release_instance()
        instance.database.close()


def test_historical_pins_keep_profiles_and_resolve_canonical_role_assets(tmp_path, monkeypatch):
    """Publish exact historical artifacts through the assembled registry and keep their saved pins."""
    from qorqut_operations.contracts import Principal
    from bazarlyk.agentic import installation
    from bazarlyk.bootstrap.seed import register_context_sources
    from host_foundation_support import build_instance, publish_role, create_registered_global, create_worker
    monkeypatch.setattr(installation.metadata, 'version', lambda _: 'test-distribution')
    instance, _ = build_instance(tmp_path, businesses=())
    defaults = Path(__file__).resolve().parents[3] / 'qorqut-agentic/src/qorqut_agentic/resources/defaults'
    legacy = Path(__file__).resolve().parents[3] / 'qorqut-roles/agent-roles'
    try:
        register_context_sources(instance.database, instance.paths)
        for role in ('ordubashy', 'global-worker', 'learning-applier'):
            instance.runtime.publisher.sources[role] = ('role', legacy, f'roles/{role}/role.yaml')
            pin = publish_role(instance, role)
            agent = create_worker(instance, 'legacy-worker') if role == 'global-worker' else create_registered_global(instance, role)
            with instance.database.read() as uow:
                before = uow.connection.execute('SELECT * FROM agent_profiles WHERE agent_id=?', (agent,)).fetchone()
                snapshot = tuple(before)
            package = instance.runtime.catalog.get('role', pin)
            assert package.ref == pin and package.manifest['brief'] == f'roles/{role}/BRIEF.md'
            view = instance.runtime.agentic_view
            canonical = view.role_brief(package)
            assert canonical['body'] == (defaults / canonical['path']).read_text()
            current = load_package(defaults, f'roles/imported-roles/native/roles/{role}/role.yaml', 'role', instance.runtime.registry)
            assert view.role_brief(current) == canonical
            selection = view.selection({'role': {'id': role}})
            for skill in package.skills:
                actual = view.skill_files('role', package, skill['id'], selection=selection)
                matching = next(item for item in current.skills if item['id'] == skill['id'])
                assert actual == view.skill_files('role', current, matching['id'], selection=selection)
            resolved = instance.runtime.resolver.resolve_agent_config(Principal(agent, 'agent')).to_dict()
            assert resolved['role'] == pin
            role_entry = next(e for e in resolved['mandatory_context']['entries'] if e['source_id'].startswith('role:'))
            assert (role_entry['source_path'], role_entry['revision'], role_entry['hash']) == (canonical['path'], canonical['revision'], canonical['sha256'])
            with instance.database.read() as uow:
                after = uow.connection.execute('SELECT * FROM agent_profiles WHERE agent_id=?', (agent,)).fetchone()
                assert tuple(after) == snapshot
    finally:
        instance.runtime.domains.shared_context.close()
        instance.runtime.release_instance()
        instance.database.close()
