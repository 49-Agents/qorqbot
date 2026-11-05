"""A synthetic host for the Context recovery cases: the real ``BackupStore`` over a real Context and Operations database.

Everything real is the product path (see ``context_completion_support``): the composed host, its Operations database with the
Context queue, filing and completion tables and the registered programs, the real ``BackgroundService`` and the real
``NightlyMaintenance`` registered as ``maintenance.nightly``, and a disposable real Git Context at the instance's own
``paths.context`` / ``paths.context_runtime``. The recovery window is real too: ``Quiescence`` wraps Operations' own
``RecoveryQuiescer`` (candidate cc8fc5f6, ``RecoveryBarrierProof``) over the host's database with the stock runtime's real
``RecoveryAdmission`` and ``InstallationRecoveryLock`` and the real ``DatabaseMaintenanceGate``; the proofs it yields are the
real ones and nothing is inferred from them. The only fakes are explicit synthetic controllers and executables:

* ``External``, ``Containers`` and ``Hosts`` stand for the writers outside this process (an external worker, containers and
  host terminals) that the host composition supplies; ``External`` really writes from a thread until it is drained.
* ``Substitute`` is the old method-shaped stand-in for a proof, kept only as the negative example the host must refuse.
* ``write_restic`` writes a small synthetic stand-in for the pinned external ``restic`` executable.
* The Context model runner and the injected clocks of the shared fixture.

Limit: this is not the stock runtime's complete writer composition (``Runtime.recovery_quiescer`` with its Docker and host
controllers); that proof follows the reviewed lane A/B/C combination. No operational data, configuration, credential or
network is read; nothing here starts a model, agent or service."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import stat
import threading
import time

import context_completion_support as support
from qorqut_context import Context
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import KnownZeroWriters, QuiescenceProof, RecoveryBarrierProof, RecoveryQuiescer

from bazarlyk.storage.recovery import BackupStore
from bazarlyk.storage.versioning import NightlyMaintenance

TABLES = ("jobs", "documents", "context_inputs", "context_input_attempts", "context_completions", "context_filings",
          "context_graph_edits", "context_entities", "context_source_bindings", "context_source_versions")


class Wall:
    """The injected wall clock of nightly capture deadlines (seconds)."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class Substitute:
    """The old method-shaped stand-in for a proof: valid only while ``active``. The host must refuse it as evidence."""

    def __init__(self, database):
        self.database, self.active = database, True

    def verify_active(self):
        if not self.active:
            raise DomainError("recovery_not_quiescent", "Recovery quiescence is no longer active")

    def require_database(self, database):
        self.verify_active()
        if database is not self.database:
            raise DomainError("recovery_not_quiescent", "Recovery quiescence does not cover this database")


class External:
    """A synthetic external writer controller: a thread that keeps writing until it is drained, restarted by ``resume``."""

    def __init__(self, name, journal):
        self.name, self.journal, self.writes, self.stopped, self.thread = name, journal, 0, None, None
        self.start()

    def start(self):
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._work, args=(self.stopped,), name=f"external-{self.name}", daemon=True)
        self.thread.start()

    def _work(self, stopped):
        while not stopped.is_set():
            self.writes += 1
            time.sleep(0.001)

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def drain(self, *, timeout):
        self.stopped.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("the external writer did not stop")
        self.journal.append(("drain", self.name))
        return QuiescenceProof(self.name, "drained")

    def resume(self):
        self.journal.append(("resume", self.name))
        self.start()


class Containers:
    def __init__(self, journal):
        self.journal = journal

    def freeze(self, *, timeout):
        self.journal.append(("freeze", "containers"))
        return QuiescenceProof("containers", "frozen")

    def thaw(self):
        self.journal.append(("thaw", "containers"))


class Hosts:
    def __init__(self, journal):
        self.journal = journal

    def inventory_and_freeze(self, *, timeout):
        self.journal.append(("freeze", "hosts"))
        return QuiescenceProof("hosts", "frozen")

    def thaw(self):
        self.journal.append(("thaw", "hosts"))


class Quiescence:
    """``runtime.recovery_quiescence`` of the synthetic host: a real Operations recovery window for one database.

    ``journal`` orders the external controllers' drain/freeze/thaw/resume; ``entries`` and ``proofs`` record each window and the
    real proof it yielded; ``calls`` counts the proofs' own ``verify_active`` and ``require_database`` invocations (the class
    methods are wrapped, never replaced). ``fail`` and ``on_enter`` are test seams around the real window.
    """

    def __init__(self, host, monkeypatch, *, database=None, admission=None, lock=None):
        stock = host.runtime.recovery_quiescer
        self.database = database or host.database
        self.admission, self.lock = admission or stock.admission, lock or stock.installation_lock
        self.journal, self.entries, self.proofs, self.calls, self.databases = [], [], [], [], []
        self.fail, self.on_enter = None, None
        self.external = External("external", self.journal)
        self.containers, self.hosts = Containers(self.journal), Hosts(self.journal)
        self.quiescer = RecoveryQuiescer(
            self.database, admission=self.admission, background=self.external, signoffs=KnownZeroWriters("signoffs"),
            email_history=KnownZeroWriters("email_history"), browser_writers=KnownZeroWriters("browser"),
            sandbox_writers=KnownZeroWriters("sandbox"), container_writers=self.containers, host_writers=self.hosts,
            installation_lock=self.lock)
        self._count(monkeypatch)

    def _count(self, monkeypatch):
        """Wrap the proof class's two methods to count calls; the wrapped behaviour is the real one."""
        verify, require = RecoveryBarrierProof.verify_active, RecoveryBarrierProof.require_database

        def counted_verify(proof):
            self.calls.append(proof)
            return verify(proof)

        def counted_require(proof, database):
            self.databases.append((proof, database))
            return require(proof, database)

        monkeypatch.setattr(RecoveryBarrierProof, "verify_active", counted_verify)
        monkeypatch.setattr(RecoveryBarrierProof, "require_database", counted_require)

    def checks(self, proof):
        """How many times ``proof.verify_active`` ran (``require_database`` runs it too)."""
        return sum(1 for item in self.calls if item is proof)

    def covered(self, proof):
        """The databases ``proof.require_database`` was asked about."""
        return [database for item, database in self.databases if item is proof]

    def close(self):
        """Stop the synthetic external writer thread."""
        self.external.stopped.set()
        self.external.thread.join(10)

    def __call__(self, **options):
        return self._quiesce(options)

    @contextmanager
    def _quiesce(self, options):
        if self.fail is not None:
            raise self.fail
        self.entries.append({"thread": threading.current_thread(), "options": options})
        with self.quiescer.quiesce(**options) as proof:
            self.proofs.append(proof)
            if self.on_enter is not None:
                self.on_enter(proof)
            yield proof


def build(tmp_path, monkeypatch, *, nightly=False):
    """The composed host with its Context at the instance's own paths, a ``BackupStore`` and optionally the nightly program."""
    wall = Wall()
    holder = {}
    if nightly:
        real = support.attach_background

        def attach(runtime, programs, **kwargs):
            holder["maintenance"] = NightlyMaintenance(runtime.database, runtime.paths, locks=runtime.locks, git_enabled=False,
                                                       wall_clock=wall)
            return real(runtime, programs, maintenance=holder["maintenance"], **kwargs)

        monkeypatch.setattr(support, "attach_background", attach)
    host = support.build(tmp_path, initialize=False)
    host.context = Context.initialize(host.paths.context, host.paths.context_runtime)
    host.restart_driver()
    host.wall, host.maintenance = wall, holder.get("maintenance")
    host.backups = host.maintenance.backups if nightly else BackupStore(host.database, host.paths)
    host.quiescence = Quiescence(host, monkeypatch)
    return host


def seed(host):
    """Published Context history (one settled filing) and one interrupted pending event with a blocked queue item."""
    ctx = host.human("first")
    host.upload(ctx, "Met Alice about the pilot.")
    assert host.step()["status"] == "published"
    host.upload(ctx, "Second note about Bob.")
    host.runner.fail = RuntimeError("stop")
    assert host.step()["status"] == "input_blocked"
    host.runner.fail = None
    assert (host.paths.context_runtime / "pending.json").is_file()
    return host


def create(host, generation, **options):
    """A generation of the host's database and Context saved under a fresh (fake) quiescence proof."""
    with host.quiescence() as proof:
        return host.backups.create(generation, barrier=proof, **options)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def authority(repository, runtime):
    """The Context's authoritative state: refs, graph/event files and event commits, and the runtime evidence files."""
    context = Context(repository, runtime)
    snapshot = context.snapshot()
    skip = {"search.sqlite3", "writer.lock", "context.json", "context-queue-driver.lock"}
    return {"refs": context.git.run("for-each-ref", "--format=%(refname) %(objectname)").decode(),
            "files": {path: sha(content) for path, content in snapshot.files.items()},
            "event_commits": dict(snapshot.event_commits),
            "runtime": {path.relative_to(runtime).as_posix(): sha(path.read_bytes()) for path in sorted(Path(runtime).rglob("*"))
                        if path.is_file() and path.name not in skip}}


def rows(database_path, tables=TABLES):
    """Every row of the named operational tables of one SQLite file, read-only and ordered."""
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
    finally:
        connection.close()


FAKE_RESTIC = r'''#!/usr/bin/env python3
"""A synthetic stand-in for the pinned restic executable: it stores copies of the named paths and restores them."""
import json, os, shutil, sys
arguments = sys.argv[1:]
repo = password = None
json_output = False
while arguments and arguments[0].startswith("--"):
    flag = arguments.pop(0)
    if flag == "--repo":
        repo = arguments.pop(0)
    elif flag == "--password-file":
        password = arguments.pop(0)
    elif flag == "--json":
        json_output = True
command = arguments.pop(0)
repo = __import__("pathlib").Path(repo) if repo else None
index = repo / "index.json" if repo else None
if command == "version":
    print("restic 0.19.1 compiled with synthetic")
elif command == "init":
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "config").write_text("synthetic")
    index.write_text("[]")
elif command == "snapshots":
    wanted = arguments[arguments.index("--tag") + 1].split(",")
    print(json.dumps([item for item in json.loads(index.read_text()) if all(tag in item["tags"] for tag in wanted)]))
elif command == "backup":
    tags, paths, time = [], [], ""
    while arguments:
        item = arguments.pop(0)
        if item == "--tag": tags.append(arguments.pop(0))
        elif item == "--time": time = arguments.pop(0)
        elif item in ("--host", "--exclude"): arguments.pop(0)
        else: paths.append(item)
    short = "%08x" % len(json.loads(index.read_text()))
    base = repo / "snapshots" / short
    for path in paths:
        target = base / path.lstrip("/")
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.copytree(path, target, symlinks=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)
    snapshots = json.loads(index.read_text())
    snapshots.append({"short_id": short, "tags": tags, "time": time, "paths": paths})
    index.write_text(json.dumps(snapshots))
elif command == "check":
    pass
elif command == "restore":
    snapshot, target = arguments[0], arguments[arguments.index("--target") + 1]
    shutil.copytree(repo / "snapshots" / snapshot, target, symlinks=True, dirs_exist_ok=True)
elif command == "forget":
    pass
else:
    sys.exit(2)
'''


def write_restic(directory):
    """The synthetic executable and its checksum, in the shape ``ResticBinary`` pins."""
    path = Path(directory) / "restic"
    path.write_text(FAKE_RESTIC)
    path.chmod(stat.S_IRWXU)
    return path, sha(path.read_bytes())
