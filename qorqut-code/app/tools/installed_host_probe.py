"""Installed-package identity and focused composition checks; invoked by check_installed_host.py under python -I."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys

config = json.loads(Path(sys.argv[1]).read_text())
checkout = Path(config['checkout']).resolve()
work = Path(config['work']).resolve()
work.mkdir(parents=True, exist_ok=True)
expected = config['expected']


def installed_identity():
    locations = {}
    for package, files in expected.items():
        module = importlib.import_module(package)
        directory = Path(module.__file__).resolve().parent
        assert Path(sys.prefix).resolve() in directory.parents, (package, directory)
        assert checkout not in directory.parents, (package, directory)
        for relative, digest in files.items():
            path = directory / relative
            assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest, (package, relative)
        locations[package] = {'directory': str(directory), 'verified_python_files': sum(1 for name in files if name.endswith('.py')),
            'verified_resource_files': sum(1 for name in files if not name.endswith('.py'))}
    for name in config['removed_modules']:
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError:
            spec = None
        assert spec is None, ('removed_module_is_installed', name)
    return locations


locations = installed_identity()
# Only fixture/test helpers are importable from source. No application directory is added to sys.path.
# Their explicit component paths select tool resources, never Python engine imports.
tests = checkout / 'app/tests'
sys.path.insert(0, str(tests))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['QORQUT_TEST_ROLE_ROOT'] = config['roles']
checks = [
 'test_combined_host_lifetime.py',
 'test_host_history_composition.py',
 'test_host_instance_ownership.py::test_another_process_cannot_take_the_lock_of_a_running_backend',
 'test_host_retained_operations.py::test_a_protected_proposal_is_applied_once_after_the_owners_exact_decision',
 'test_host_retained_operations.py::test_a_non_context_program_runs_only_under_its_registered_worker_authority',
 'test_host_retained_operations.py::test_chat_reply_read_and_attention_metadata_stay_independent',
 'test_host_parent_tasks.py::test_completing_or_reopening_a_parent_never_changes_its_children',
 'test_host_parent_tasks.py::test_a_retained_signoff_applies_the_exact_task_backed_campaign_plan',
 'test_host_parent_tasks.py::test_the_active_operation_catalog_has_no_story_handlers',
 'test_host_context_services.py::test_an_upload_and_an_action_revision_reach_one_ordered_queue_and_publish_with_exact_receipts',
 'test_host_business_create.py::test_the_configured_ordubashy_creates_an_empty_project_from_a_name_alone_and_the_owner_keeps_it',
 'test_host_business_create.py::test_the_owner_is_refused_over_the_real_http_endpoint_and_by_direct_calls_with_nothing_changed',
 'test_host_instruction_seeds.py',
 'test_context_recovery_host.py::test_restore_into_other_paths_reproduces_the_authoritative_hashes_and_the_exact_pending_state',
 'test_nightly_capture_host.py::test_the_coordinator_captures_under_quiescence_and_the_job_then_returns_its_one_final_result',
 'test_inbox_nudges.py::test_a_busy_session_gets_the_notice_at_once_without_inferring_processing',
 'test_inbox_nudges.py::test_an_offline_event_waits_for_a_manual_session_start_and_nothing_launches_one',
 'test_inbox_nudges.py::test_an_uncertain_submission_survives_a_restart_and_is_never_resent',
]
checks = config.get("checks", checks)
import pytest
output = io.StringIO()
with redirect_stdout(output), redirect_stderr(output):
    result = pytest.main(['-q', '-c', '/dev/null', '-o', 'pythonpath=', '-p', 'no:cacheprovider',
        '--basetemp', str(work / 'synthetic-instances'), '--maxfail=4', *[str(tests / case) for case in checks]])
(work / 'focused-checks.log').write_text(output.getvalue())
locations = installed_identity()
for name, module in tuple(sys.modules.items()):
    if name.split('.')[0] in expected and getattr(module, '__file__', None):
        assert Path(sys.prefix).resolve() in Path(module.__file__).resolve().parents, (name, module.__file__)
evidence = {'format': 'qorqut-v1.2-installed-check', 'host_commit': config['host_commit'],
    'operations_commit': config['engine_commit'], 'context_commit': config['context_commit'],
    'dashboard_commit': config['dashboard_commit'], 'dirty': config['dirty'],
    'passed': result == 0, 'pytest_exit': int(result), 'checks': checks, 'packages': locations,
    'details': str(work / 'focused-checks.log'), 'provider_validation': 'synthetic adapters and injected runners only',
    'live_state_touched': False, 'migration_executed': False}
(work / 'receipt.json').write_text(json.dumps(evidence, indent=2) + '\n')
print(json.dumps(evidence))
if result:
    print(output.getvalue(), file=sys.stderr)
    raise SystemExit(result)
