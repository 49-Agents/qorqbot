"""Scheduled business database pulls, with no live endpoint or business data."""
import gzip
import io
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from bazarlyk.background.backups import BackupDownloader, BackupPullProgram
from qorqut_operations.contracts import DomainError
from test_tables import service, call, ok


def database_bytes(tmp_path, rows=1):
    path = tmp_path/"source.sqlite3"
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    with con:
        con.execute("PRAGMA journal_mode=WAL")  # Like a live product database's online backup.
        con.execute("CREATE TABLE users(id TEXT)")
        con.executemany("INSERT INTO users VALUES(?)", [(str(i),) for i in range(rows)])
    con.close()
    return gzip.compress(path.read_bytes())


def use(service, downloader):
    program = service.runtime.background.programs.require("backups.pull")
    pull = BackupPullProgram(downloader).run
    object.__setattr__(program, "run", pull)
    object.__setattr__(program, "recover", pull)


def routine(service, **arguments):
    definition = {"name": "Database backup", "program": "backups.pull", "schedule": "17 */6 * * *",
                  "timezone": "UTC", "status": "paused",
                  "arguments": {"endpoint_env": "APP_BACKUP_URL", "token_env": "APP_BACKUP_TOKEN", **arguments}}
    return ok(call(service, "routines_create", {"definition": definition}))["records"][0]


def run(service, created):
    job_id = ok(call(service, "routines_run", {"routine_id": created["id"]}, created["revision"]))["job_ids"][0]
    service.runtime.background.run_one(job_id)
    with service.fixture.db.read() as uow:
        return uow.repository("jobs", business_id=service.fixture.business_id).get(job_id)


def folder(service):
    return service.fixture.root/".bazarlyk"/"business-backups"/service.fixture.business_id


def test_routine_keeps_verified_private_copies_and_rotates(service, tmp_path):
    calls = []
    def downloader(business_id, endpoint_env, token_env, stream, timeout):
        calls.append((business_id, endpoint_env, token_env))
        stream.write(database_bytes(tmp_path, rows=len(calls)))
    use(service, downloader)
    created = routine(service, keep=2)
    for _ in range(3):
        job = run(service, created)
        assert job["state"] == "succeeded", job
    assert calls[0] == (service.fixture.business_id, "APP_BACKUP_URL", "APP_BACKUP_TOKEN")
    copies = sorted(folder(service).iterdir(), key=lambda p: p.stat().st_mtime_ns)
    assert len(copies) == 2 and not list(folder(service).glob(".*")) and all(p.name.endswith(".sqlite3.gz") for p in copies)
    assert stat.S_IMODE(folder(service).stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in copies)
    restored = tmp_path/"restored.sqlite3"
    restored.write_bytes(gzip.decompress(copies[-1].read_bytes()))
    with sqlite3.connect(restored) as con:
        assert con.execute("SELECT count(*) FROM users").fetchone()[0] == 3
    assert ok(call(service, "routines_runs", {"routine_id": created["id"]}))["items"][0]["state"] == "succeeded"


@pytest.mark.parametrize("content", [b"not gzip", gzip.compress(b"not a database"), b""])
def test_unreadable_backup_fails_and_keeps_nothing(service, content):
    use(service, lambda business_id, endpoint_env, token_env, stream, timeout: stream.write(content))
    job = run(service, routine(service))
    assert job["state"] == "failed", job
    assert list(folder(service).iterdir()) == []  # Neither a copy nor a partial download remains.


def test_recovered_job_does_not_download_twice(service, tmp_path):
    calls = []
    def downloader(business_id, endpoint_env, token_env, stream, timeout):
        calls.append(1)
        stream.write(database_bytes(tmp_path))
    control = SimpleNamespace(service=SimpleNamespace(businesses_root=service.fixture.root),
                              business_id=service.fixture.business_id,
                              job={"id": "0" * 32, "created_at": "2026-09-25T06:17:00Z"},
                              check=lambda: None, remaining_seconds=lambda: 60)
    program = BackupPullProgram(downloader)
    arguments = {"endpoint_env": "APP_BACKUP_URL", "token_env": "APP_BACKUP_TOKEN"}
    first, again = program.run(control, arguments), program.run(control, arguments)
    assert first.data == again.data and first.data["file"] == "20260925T061700Z-00000000.sqlite3.gz"
    assert len(calls) == 1


def test_routine_arguments_name_secrets_not_values(service):
    for bad in ({"endpoint_env": "https://example.test/", "token_env": "APP_BACKUP_TOKEN"},
                {"endpoint_env": "APP_BACKUP_URL"}, {"endpoint_env": "APP_BACKUP_URL", "token_env": "T", "keep": 0}):
        definition = {"name": "Backup", "program": "backups.pull", "schedule": "0 * * * *", "timezone": "UTC",
                      "status": "paused", "arguments": bad}
        assert call(service, "routines_create", {"definition": definition})["status"] == "error", bad


def test_downloader_uses_scoped_secrets_and_never_exposes_them(monkeypatch):
    import httpx
    original, requests = httpx.Client, []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b"backup-bytes")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    env = {"URL": "https://app.example.test/ops/backup", "TOKEN": "private-token"}
    downloader = BackupDownloader(lambda business: env)
    stream = io.BytesIO()
    assert downloader("business", "URL", "TOKEN", stream, 30) == len(b"backup-bytes")
    assert stream.getvalue() == b"backup-bytes"
    assert requests[0].headers["authorization"] == "Bearer private-token"
    for response in (httpx.Response(302, headers={"Location": "https://other.test/"}), httpx.Response(404)):
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(lambda r: response), **kwargs))
        with pytest.raises(DomainError) as error:
            downloader("business", "URL", "TOKEN", io.BytesIO(), 30)
        assert "private-token" not in str(error.value) and "example.test" not in str(error.value)
    for broken in ({"URL": "http://app.example.test/", "TOKEN": "t"}, {"URL": "https://u:p@app.example.test/", "TOKEN": "t"},
                   {"URL": "https://app.example.test/"}):
        with pytest.raises(DomainError) as error:
            BackupDownloader(lambda business: broken)("business", "URL", "TOKEN", io.BytesIO(), 30)
        assert error.value.code == "invalid_configuration"
