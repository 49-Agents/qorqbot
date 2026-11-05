"""Actual host services and capture, with an isolated empty native-process inventory and no providers."""
from uuid import uuid4
from qorqut_operations.storage.quiescence import BoundedWriter
from bazarlyk.sandbox.service import BusinessSandboxService
from bazarlyk.sandbox.preview_service import BusinessPreviews
from bazarlyk.sandbox.quiescence import DockerFreezer

from qorqut_context import Context
from bazarlyk.background.integration import attach_background
from bazarlyk.sandbox.legacy_writers import HostTerminalWriters, LegacyBrowserWriters, LegacyHostWriters
from bazarlyk.storage.versioning import NightlyMaintenance
from host_foundation_support import build_instance


def test_stock_services_capture_and_resume_without_replacing_context_or_history(tmp_path):
    host, _ = build_instance(tmp_path, businesses=(), owned=False)
    runtime = host.runtime
    Context.initialize(host.paths.context, host.paths.context_runtime)
    maintenance = NightlyMaintenance(runtime.database, runtime.paths, locks=runtime.locks)
    attach_background(runtime, runtime.programs, maintenance=maintenance)
    runtime.maintenance_capture = maintenance.bind_runtime(runtime)
    # The same inventory controllers as main(), using a unique socket with no server or agents.
    runtime.recovery_quiescer.host_writers = LegacyHostWriters(
        HostTerminalWriters.isolated_for_test('/usr/bin/tmux', 'v12-check-' + uuid4().hex),
        LegacyBrowserWriters(runtime.database, runtime.paths))
    runtime.sandbox = BusinessSandboxService(runtime)
    runtime.previews = BusinessPreviews(runtime, runtime.sandbox.docker, ('http://127.0.0.1',))
    runtime.recovery_quiescer.sandbox_writers = BoundedWriter(runtime.sandbox.worker, 'sandbox')
    runtime.recovery_quiescer.browser_writers = BoundedWriter(runtime.previews, 'private_previews')
    runtime.recovery_quiescer.container_writers.freezer = DockerFreezer(runtime.sandbox.docker)
    runtime.freeze_registry()
    context, history = runtime.context_services, runtime.history_observer
    runtime.start()
    try:
        assert {'context', 'history', 'background', 'native_chat', 'inbox_nudges', 'maintenance_capture'} <= runtime.running
        with runtime.recovery_quiescence(timeout=30) as proof:
            proof.require_database(runtime.database)
            captured = maintenance.backups.create(str(uuid4()), barrier=proof)
        assert captured['context']['manifest_sha256']
        assert runtime.context_services is context and runtime.history_observer is history
        assert context.running and history.state().started and not history.state().stopping
    finally:
        result = runtime.stop()
    assert result.clean and result.database_closed and result.ownership_released
