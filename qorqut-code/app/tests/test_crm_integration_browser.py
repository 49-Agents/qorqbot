"""Actual CRM dashboard requests against a disposable composed Host/Operations application."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from project_composition_support import DASHBOARD, build_composed, write_fixture

PLAYWRIGHT = os.environ.get("PLAYWRIGHT_MODULE", "/tmp/node_modules/playwright")

@pytest.mark.skipif(shutil.which("node") is None or not Path(PLAYWRIGHT).is_dir(), reason="node/playwright is unavailable")
def test_global_and_project_crms_through_the_real_browser_and_http(tmp_path):
    host = build_composed(tmp_path)
    try:
        fixture, result, shots = tmp_path / 'fixture.json', tmp_path / 'result.json', tmp_path / 'shots'
        shots.mkdir()
        write_fixture(host, fixture)
        environment = {**os.environ, 'CRM_COMPOSITION_BASE': host.origin,
                       'PLAYWRIGHT_MODULE': PLAYWRIGHT,
                       'CRM_COMPOSITION_FIXTURE': str(fixture), 'CRM_COMPOSITION_RESULT': str(result),
                       'CRM_COMPOSITION_SHOTS': str(shots)}
        completed = subprocess.run(['node', str(DASHBOARD / 'tests/test_crm_composition_browser.js')],
                                   env=environment, capture_output=True, text=True, timeout=240)
        print(completed.stdout)
        print(completed.stderr)
        assert completed.returncode == 0
        records = json.loads(result.read_text())['records']
        assert len(records) == 2
        with host.database.read() as uow:
            for record in records:
                member = uow.connection.execute('SELECT stage_id,cells_json FROM crm_entries WHERE table_id=? AND record_task_id=?',
                                                (record['table_id'], record['task_id'])).fetchone()
                assert member['stage_id'] == 'contacted'
                assert json.loads(member['cells_json'])['next_step'] == 'Review the brief'
                count = uow.connection.execute('SELECT COUNT(*) FROM crm_activity_events WHERE business_id IS ? AND task_id=?',
                                               (record['business_id'], record['task_id'])).fetchone()[0]
                assert count == (1 if record['business_id'] else 0)
    finally:
        host.close()
