"""Read-only, pre-input Codex binding on the existing native setup tick (D51)."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Literal
from uuid import UUID

from ..adapters.manual_hooks import NativeProgram
from ..app import _check_actor
from ..contracts import Invocation, canonical
from ..runner.models import ProcessFingerprint
from ..runner.process import fingerprint, verify_process
from ..runner.shell_models import ShellView
from ..storage.database import assert_external_io_allowed
from ..storage.events import EventInput, append_event
from .models import OwnerCandidate, SessionConflict
from .native_hooks import _native_process, _node
from .owners import _active, _binding, _installation

_PROC = Path("/proc")
_MAX_PROCESSES = 256
_MAX_DEPTH = 32
_MAX_DESCRIPTORS = 256


@dataclass(frozen=True)
class _StartupResult:
    outcome: Literal["connected", "reconnected", "observed", "conflict", "unavailable"]
    owner_id: UUID | None = None
    run_generation: int | None = None

    @property
    def reason(self):
        return {"connected": "startup_observed", "reconnected": "startup_observed",
                "observed": "startup_observed", "conflict": "session_conflict",
                "unavailable": "observation_unavailable"}[self.outcome]


def _read(path, limit):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Process facts exceed observation bound")
    return raw.decode("ascii")


def _children(pid):
    values = _read(_PROC / str(pid) / "task" / str(pid) / "children", 4096).split()
    if len(values) > _MAX_PROCESSES or any(not v.isdecimal() or int(v) <= 0 for v in values):
        raise ValueError("Invalid bounded process children")
    return tuple(int(v) for v in values)


def _discover(shell: ProcessFingerprint, programs: tuple[NativeProgram, ...]):
    executables = {str(p.executable.resolve(strict=True)) for p in programs}
    pending, seen, facts, selected = [(shell, None, 0)], {shell.pid}, [], []
    while pending:
        process, expected_parent, depth = pending.pop()
        parent, executable = _node(process)
        if (expected_parent is not None and parent != expected_parent
                or process.boot_id != shell.boot_id or process.uid != shell.uid
                or process.pid_namespace != shell.pid_namespace):
            raise ValueError("Descendant differs from recorded shell")
        children = _children(process.pid)
        facts.append((process, parent, executable, children))
        if process != shell and executable in executables:
            selected.append((process, executable))
        for pid in children:
            if depth == _MAX_DEPTH or pid in seen or len(seen) == _MAX_PROCESSES:
                raise ValueError("Unverifiable bounded descendant tree")
            seen.add(pid)
            pending.append((fingerprint(pid), process.pid, depth + 1))
    if len(selected) != 1:
        raise ValueError("No unique configured native root")
    process, executable = selected[0]
    matching = [p for p in programs if str(p.executable.resolve(strict=True)) == executable]
    if len(matching) != 1 or (matching[0].provider, matching[0].cli_version) != ("codex", "0.160.0"):
        raise ValueError("Startup observation requires the pinned Codex profile")
    for observed, parent, executable, children in reversed(facts):
        if _node(observed) != (parent, executable) or _children(observed.pid) != children:
            raise ValueError("Descendants changed during inspection")
    program = matching[0]
    if _native_process(process, shell, program, programs, include_start=True) != process:
        raise ValueError("Native root changed")
    return process, program


@dataclass(frozen=True)
class _WriterLock:
    session_id: UUID
    descriptor: str
    device: int
    inode: int


def _lock_at(process, home, descriptor):
    fd = _PROC / str(process.pid) / "fd" / descriptor
    target = fd.readlink()
    directory = home / ".codex/thread-writer-locks"
    if target.parent != directory or target.name == ".coordination.lock" or target.suffix != ".lock":
        return None
    session = UUID(target.stem)
    if target.name != f"{session}.lock" or target.resolve(strict=True) != target:
        raise ValueError("Noncanonical native writer lock")
    named, opened = target.lstat(), fd.stat()
    if (not stat.S_ISREG(named.st_mode) or not stat.S_ISREG(opened.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)):
        raise ValueError("Native writer descriptor differs from named lock")
    lines = [line.split() for line in _read(_PROC / str(process.pid) / "fdinfo" / descriptor, 65536).splitlines()]
    if [line for line in lines if line[:1] == ["ino:"]] != [["ino:", str(opened.st_ino)]]:
        raise ValueError("Native writer fdinfo inode differs")
    # FLOCK/device/inode/PID proof follows bazarlyk/bootstrap/codex_session.py
    # at PORTING pin e71fd1e527368bfd41b9815126f4bf33452aeebb. No live import,
    # lock acquisition, payload read or native-file mutation is involved.
    held = []
    for fields in lines:
        if fields[:1] != ["lock:"]:
            continue
        if (len(fields) != 9 or fields[2:6] != ["FLOCK", "ADVISORY", "WRITE", str(process.pid)]
                or fields[7:] != ["0", "EOF"]):
            raise ValueError("Native writer lacks an exclusive whole-file FLOCK")
        major, minor, inode = fields[6].split(":")
        held.append((int(major, 16), int(minor, 16), int(inode)))
    if held != [(os.major(opened.st_dev), os.minor(opened.st_dev), opened.st_ino)]:
        raise ValueError("Native writer lock is not held by the exact process")
    if fd.readlink() != target or fd.stat() != opened or target.lstat() != named:
        raise ValueError("Native writer lock changed during inspection")
    return _WriterLock(session, descriptor, opened.st_dev, opened.st_ino)


def _writer_lock(process, home):
    matches, count = [], 0
    with os.scandir(_PROC / str(process.pid) / "fd") as descriptors:
        for entry in descriptors:
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            count += 1
            if count > _MAX_DESCRIPTORS:
                raise ValueError("Native descriptor bound exceeded")
            proof = _lock_at(process, home, entry.name)
            if proof is not None:
                matches.append(proof)
    if len(matches) != 1:
        raise ValueError("No unique held native session writer lock")
    return matches[0]


def _payload(candidate, owner):
    return {**candidate.model_dump(mode="json", exclude={"process"}),
            "owner_id": owner["owner_id"], "run_generation": owner["run_generation"],
            "native_process": candidate.process.model_dump(mode="json"), "evidence_kind": "codex_writer_lock"}


def _recorded(tx, context, candidate, owner):
    return tx.connection.execute(
        "SELECT 1 FROM events WHERE kind='native.startup_observed' AND actor_id=? AND agent_id=? "
        "AND payload_json=? LIMIT 1", (str(context.actor_id), str(candidate.agent_id), canonical(_payload(candidate, owner)))
    ).fetchone() is not None


class NativeStartup:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def _target(self, tx, context, terminal):
        _check_actor(tx, context)
        spec = terminal.spec
        agent = self.app.session_owners._agent(tx, spec.agent_id, admission=True)
        root = self.app.settings.data_root / "agents" / agent["id"]
        installation = _installation(tx)
        if (str(context.actor_id) != agent["actor_id"]
                or installation != (str(spec.installation_id), str(spec.recovery_epoch))
                or spec.installation_id != self.app.settings.installation_id
                or spec.home != root / "runtime/home" or spec.workspace != root / "workspace"):
            raise ValueError("Startup target binding changed")
        return installation, tuple(agent[k] for k in ("id", "actor_id", "conversation_id", "lifecycle",
            "lifecycle_revision", "provisioning_job_id", "workspace_initialized_at"))

    def observe(self, context: Invocation, *, terminal: ShellView,
                programs: tuple[NativeProgram, ...], guard) -> _StartupResult:
        try:
            assert_external_io_allowed()
            with self.db.lock.use(), self.db.lock.effects:
                if terminal.state != "running" or terminal.shell is None:
                    raise ValueError("No recorded live shell")
                with self.db.read() as tx:
                    before = self._target(tx, context, terminal)
                guard()
                native, program = _discover(terminal.shell, programs)
                proof = _writer_lock(native, terminal.spec.home)
                candidate = OwnerCandidate(agent_id=terminal.spec.agent_id, terminal_id=terminal.terminal_id,
                    recovery_epoch=terminal.spec.recovery_epoch, environment_generation=terminal.spec.environment_generation,
                    provider=program.provider, cli_version=program.cli_version, native_session_id=proof.session_id, process=native)
                guard()
                current = self.app.terminals.native_context(candidate.agent_id, candidate.terminal_id)
                if current.spec != terminal.spec or current.shell != terminal.shell or current.state != "running":
                    raise ValueError("Recorded shell changed during observation")
                if (_native_process(native, terminal.shell, program, programs, include_start=True) != native
                        or _writer_lock(native, terminal.spec.home) != proof):
                    raise ValueError("Native proof changed before admission")
                verify_process(native)
                verify_process(terminal.shell)
                with self.db.read() as tx:
                    if self._target(tx, context, terminal) != before:
                        raise ValueError("Startup target changed before admission")
                    owner = _active(tx, candidate.agent_id)
                    if (owner is not None and all(owner[k] == v for k, v in _binding(candidate).items())
                            and _recorded(tx, context, candidate, owner)):
                        return _StartupResult("observed", UUID(owner["owner_id"]), owner["run_generation"])
                connected = self.app.session_owners.connect(candidate)
                guard()
                with self.db.transaction() as tx:
                    if self._target(tx, context, terminal) != before:
                        raise ValueError("Startup target changed before recording")
                    owner = _active(tx, candidate.agent_id)
                    if (owner is None or owner["owner_id"] != str(connected.owner_id)
                            or owner["run_generation"] != connected.run_generation
                            or any(owner[k] != v for k, v in _binding(candidate).items())):
                        raise ValueError("Startup owner changed before recording")
                    if not _recorded(tx, context, candidate, owner):
                        append_event(tx, EventInput(kind="native.startup_observed", actor_id=context.actor_id,
                            agent_id=candidate.agent_id, payload=_payload(candidate, owner)))
                return _StartupResult(connected.outcome, connected.owner_id, connected.run_generation)
        except SessionConflict:
            return _StartupResult("conflict")
        except Exception:
            # Missing/ambiguous/changed proof is unavailable, never a conflict,
            # owner release, native configuration failure or readiness grant.
            return _StartupResult("unavailable")
