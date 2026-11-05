"""Revision child deadline, protocol failure and process lifetime."""
import subprocess
import time

import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.bootstrap import catalog_revision


@pytest.mark.parametrize('code', [
    "import sys; sys.stdout.write('not-json\\n'); sys.stdout.flush()",
    "import sys; sys.stdout.write('{\"revision\":\"a\",\"revision\":\"b\"}\\n'); sys.stdout.flush()",
    "pass",
    "import time; time.sleep(30)",
    "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(30)",
    "import sys; sys.stdout.write('x'*5000+'\\n'); sys.stdout.flush()",
])
def test_bad_or_stalled_observer_is_pending_and_reaped(tmp_path, monkeypatch, code):
    monkeypatch.setattr(catalog_revision, '_OBSERVER_CODE', code)
    original = subprocess.Popen
    processes = []
    def spawn(*args, **kwargs):
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(catalog_revision.subprocess, 'Popen', spawn)
    started = time.monotonic()
    with pytest.raises(DomainError) as error:
        with catalog_revision.open_observer(tmp_path, started + .3) as observer:
            observer.observe()
    assert error.value.code == 'bootstrap_pending'
    assert time.monotonic() - started < 1.5
    assert len(processes) == 1 and processes[0].poll() is not None


def test_one_deadline_bounds_both_observations(tmp_path, monkeypatch):
    code = "import sys,time,json\nfor line in sys.stdin:\n time.sleep(.18); print(json.dumps({'revision':'a'*64}), flush=True)"
    monkeypatch.setattr(catalog_revision, '_OBSERVER_CODE', code)
    started = time.monotonic()
    with catalog_revision.open_observer(tmp_path, started + .3) as observer:
        assert observer.observe() == 'a'*64
        with pytest.raises(DomainError) as error:
            observer.observe()
        assert error.value.code == 'bootstrap_pending'
    assert time.monotonic() - started < 1.5
    assert observer.process.poll() is not None
