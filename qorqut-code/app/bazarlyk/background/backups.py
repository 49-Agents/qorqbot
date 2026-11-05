"""Scheduled pulls of a business's own database backup; configuration never loads executable code.

A business routine runs `backups.pull` with the names of two business secrets: an HTTPS endpoint
that returns a gzipped SQLite database, and a bearer token for it. The copy must decompress and
pass SQLite's quick check before it is kept, privately, under
`<instance runtime>/business-backups/<business id>/`, outside Git, the agents' workspace
and the nightly authored backup. The newest `keep` copies are retained.
"""
from datetime import datetime
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from qorqut_operations.contracts import DomainError
from qorqut_operations.application.plans import schema
from qorqut_operations.background.programs import ProgramResult

ENV = {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{0,99}$"}
ARGUMENTS = schema({"endpoint_env": ENV, "token_env": ENV, "keep": {"type": "integer", "minimum": 1, "maximum": 365}},
                   ("endpoint_env", "token_env"))
DEFAULT_KEEP = 30
MAX_BYTES = 1024 ** 3
SUFFIX = ".sqlite3.gz"


class BackupDownloader:
    """Stream one backup to a file, using the business's own secrets and egress route."""
    def __init__(self, environment, *, proxy_resolver=None):
        self.environment, self.proxy_resolver = environment, proxy_resolver

    def __call__(self, business_id, endpoint_env, token_env, stream, timeout):
        env = self.environment(business_id)
        endpoint, token = env.get(endpoint_env, ""), env.get(token_env, "")
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise DomainError("invalid_configuration", "Backup endpoint must be an HTTPS URL without embedded credentials")
        if not token:
            raise DomainError("invalid_configuration", "Backup token is not configured")
        proxy = self.proxy_resolver(business_id) if self.proxy_resolver else None
        budget = max(1, min(timeout, 600))
        deadline, size = time.monotonic() + budget, 0
        try:
            with httpx.Client(timeout=min(budget, 60), follow_redirects=False, trust_env=False, proxy=proxy) as client:
                with client.stream("GET", endpoint, headers={"Authorization": "Bearer " + token}) as response:
                    if response.status_code != 200:
                        raise DomainError("collection_failed", f"Backup endpoint returned HTTP {response.status_code}")
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise DomainError("timeout", "Backup download exceeded its time budget")
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise DomainError("result_too_large", "Backup exceeds 1 GiB")
                        stream.write(chunk)
        except httpx.HTTPError:
            # Never expose credential-bearing URLs, tokens or response bodies in routine errors.
            raise DomainError("collection_failed", "Backup request failed") from None
        return size


def verify(path, scratch):
    """Raise unless `path` is a gzipped SQLite database that passes its quick check."""
    try:
        with gzip.open(path, "rb") as source, open(scratch, "wb") as target:
            shutil.copyfileobj(source, target)
        # immutable: a WAL-mode copy must not grow -wal/-shm files next to the kept backups.
        with sqlite3.connect(f"file:{scratch}?mode=ro&immutable=1", uri=True) as con:
            ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    except (OSError, EOFError, sqlite3.Error):
        ok, tables = False, 0
    finally:
        for leftover in ("", "-wal", "-shm", "-journal"):
            Path(str(scratch) + leftover).unlink(missing_ok=True)
    if not ok or not tables:
        raise DomainError("invalid_backup", "Downloaded backup is not a readable SQLite database")


def private_folder(runtime_directory, business_id):
    folder = Path(runtime_directory)/"business-backups"/business_id
    for path in (folder.parent.parent, folder.parent, folder):
        if path.is_symlink():
            raise DomainError("invalid_configuration", "Project backups must not pass through a symlink")
        path.mkdir(mode=0o700, exist_ok=True)
    return folder


class BackupPullProgram:
    def __init__(self, downloader=None):
        self.downloader = downloader

    def run(self, control, args):
        control.check()
        if self.downloader is None:
            raise DomainError("operation_unavailable", "Backup downloader is not configured")
        folder = private_folder(control.service.paths.runtime, control.business_id)
        job = control.job["id"]
        created = datetime.fromisoformat(control.job["created_at"].replace("Z", "+00:00"))
        name = created.strftime("%Y%m%dT%H%M%SZ") + "-" + job[:8] + SUFFIX
        final = folder/name
        if not final.exists():  # A recovered job that already kept its copy does not download again.
            incoming = folder/(".incoming-" + job)
            descriptor = os.open(incoming, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    self.downloader(control.business_id, args["endpoint_env"], args["token_env"], stream,
                                    control.remaining_seconds())
                    stream.flush()
                    os.fsync(stream.fileno())
                control.check()
                verify(incoming, folder/(".verify-" + job))
                os.replace(incoming, final)
                directory = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                incoming.unlink(missing_ok=True)
        size = final.stat().st_size
        # Oldest first by write time; this job's copy is never the one removed.
        copies = sorted((p for p in folder.iterdir() if p.name.endswith(SUFFIX) and not p.is_symlink() and p != final),
                        key=lambda p: (p.stat().st_mtime_ns, p.name))
        removed = copies[:max(0, len(copies) + 1 - args.get("keep", DEFAULT_KEEP))]
        for path in removed:
            path.unlink(missing_ok=True)
        return ProgramResult(data={"file": name, "bytes": size, "kept": len(copies) + 1 - len(removed),
                                   "removed": len(removed)})
