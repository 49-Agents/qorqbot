"""D51: private synthetic proc facts; no native launch, input or lock effects."""

from contextlib import ExitStack, contextmanager
from functools import partial
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from qorqut_core.adapters.manual_hooks import NativeProgram
from qorqut_core.contracts import Invocation
from qorqut_core.runner.process import fingerprint, verify_process
from qorqut_core.runner.shell_models import ShellControl, ShellSpec, ShellView
from qorqut_core.runtime import native_hooks, native_startup
from qorqut_core.transcripts.probe import ProbeResult
from runtime.support import RuntimeCase


class Proc:
    def __init__(self, case):
        self.case = case
        self.root = case.root / uuid4().hex
        self.root.mkdir()
        boot = self.root / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text(str(uuid4()))
        self.namespace = self.root / "namespace"
        self.namespace.touch()
        self.programs = tuple(NativeProgram(provider=p, cli_version=v, executable=self.root / p)
                              for p, v in (("codex", "0.160.0"), ("claude", "2.1.287")))
        self.wrapper = self.root / "wrapper"
        self.wrapper.touch()
        for program in self.programs:
            program.executable.touch()
        self.node(100, 1, self.wrapper, (101,))
        self.node(101, 100, self.wrapper, (102,))
        self.node(102, 101, self.programs[0].executable)
        agent = case.settings.data_root / "agents" / case.host["id"]
        self.home = agent / "runtime/home"
        self.home.mkdir(parents=True, exist_ok=True)
        spec = ShellSpec(installation_id=case.settings.installation_id, agent_id=case.host["id"],
            recovery_epoch=case.config()["recovery_epoch"], environment_generation=1,
            home=self.home, workspace=agent / "workspace", path=os.defpath)
        self.shell = ShellView(spec=spec, terminal_id=uuid4(), state="running", reason="fixture",
            shell=fingerprint(100, proc_root=self.root), source_id=uuid4(), source_epoch=uuid4(),
            probe_address="fixture", capture=ProbeResult(verified=False, terminal_active=False, reason="unavailable"),
            control=ShellControl(mode="unclaimed", epoch=0, viewer_id=None, last_sequence=0))
        self.session = uuid4()
        self.lock = self.writer(self.session)
        self.info = self.root / "102/fdinfo/7"

    def node(self, pid, parent, executable, children=()):
        root = self.root / str(pid)
        for name in ("ns", "fd", "fdinfo", f"task/{pid}"):
            (root / name).mkdir(parents=True, exist_ok=True)
        fields = ["S", str(parent), *(["0"] * 17), str(pid * 10)]
        (root / "stat").write_text(f"{pid} (synthetic (process)) " + " ".join(fields))
        (root / "ns/pid").symlink_to(self.namespace)
        (root / "exe").symlink_to(executable)
        self.children(pid, children)

    def children(self, pid, values):
        (self.root / str(pid) / f"task/{pid}/children").write_text(" ".join(str(p) for p in values))

    def writer(self, session, descriptor="7", pid=102):
        lock = self.home / ".codex/thread-writer-locks" / f"{session}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"ignored payload is not session identity")
        (self.root / str(pid) / "fd" / descriptor).symlink_to(lock)
        info = lock.stat()
        (self.root / str(pid) / "fdinfo" / descriptor).write_text(
            f"pos:\t0\nflags:\t0100002\nino:\t{info.st_ino}\n"
            f"lock:\t1: FLOCK ADVISORY WRITE {pid} {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n")
        return lock

    @contextmanager
    def patched(self):
        with ExitStack() as stack:
            for module in (native_startup, native_hooks):
                stack.enter_context(patch.object(module, "fingerprint", partial(fingerprint, proc_root=self.root)))
                stack.enter_context(patch.object(module, "verify_process", partial(verify_process, proc_root=self.root)))
            stack.enter_context(patch.object(native_startup, "_PROC", self.root))
            stack.enter_context(patch.object(native_hooks, "Path", lambda value: self.root if value == "/proc" else Path(value)))
            stack.enter_context(patch.object(self.case.app.terminals, "native_context", return_value=self.shell))
            stack.enter_context(patch.object(self.case.app.session_owners, "observe_process", self.case.observe))
            yield

    def observe(self, *, guard=lambda: None, **changes):
        with self.patched():
            return self.case.app.native_setup._startup.observe(
                Invocation(actor_id=self.case.host["actor_id"]), terminal=changes.get("terminal", self.shell),
                programs=changes.get("programs", self.programs), guard=guard)


class Startup(RuntimeCase):
    def startup_events(self):
        return self.events(self.host["id"], "native.startup_observed")

    def test_exact_binding_once_then_real_hook_and_restart_keep_owner(self):
        proc = Proc(self)
        # An unrelated, deliberately unreadable process is never inspected.
        (proc.root / "999").mkdir()
        (proc.root / "102/fd/8").symlink_to(proc.home / ".codex/thread-writer-locks/.coordination.lock")
        before = proc.lock.read_bytes(), proc.lock.stat().st_ino
        with patch.object(self.app.session_owners, "connect", wraps=self.app.session_owners.connect) as connect:
            admitted = proc.observe()
            self.assertEqual(admitted.outcome, "connected")
            for _ in range(3):
                self.assertEqual(proc.observe().outcome, "observed")
            self.assertEqual(connect.call_count, 1)
        owner = self.rows("session_owners")[0]
        self.assertEqual(owner["native_session_id"], str(proc.session))
        self.assertEqual(json.loads(owner["process_json"]), fingerprint(102, proc_root=proc.root).model_dump(mode="json"))
        self.assertEqual((owner["terminal_id"], owner["run_generation"]), (str(proc.shell.terminal_id), 1))
        event = self.startup_events()[0]
        self.assertEqual(event["actor_id"], self.host["actor_id"])
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["evidence_kind"], "codex_writer_lock")
        self.assertEqual(payload["owner_id"], owner["owner_id"])
        self.assertFalse({"activity", "event", "transcript_path", "input_ready"} & payload.keys())
        self.assertFalse(self.events(self.host["id"], "native.hook"))
        self.assertFalse(self.runtime()["input_ready"])
        self.assertEqual(before, (proc.lock.read_bytes(), proc.lock.stat().st_ino))
        # Synthetic delivery of the actual hook contract reconnects that owner.
        proc.children(102, (103,))
        proc.node(103, 102, proc.wrapper)
        request = native_hooks.NativeHookRequest(provider="codex", agent_id=self.host["id"],
            terminal_id=proc.shell.terminal_id, hook_process=fingerprint(103, proc_root=proc.root),
            payload={"hook_event_name": "SessionStart", "session_id": str(proc.session),
                     "transcript_path": str(proc.home / f".codex/sessions/{proc.session}.jsonl")})
        with proc.patched():
            hook = self.app.native_hooks.handle(Invocation(actor_id=self.host["actor_id"]), request,
                terminal=proc.shell, program=proc.programs[0], configured_programs=proc.programs)
        self.assertEqual((hook.outcome, hook.owner_id, hook.run_generation), ("reconnected", admitted.owner_id, 1))
        self.restart()
        self.assertEqual(proc.observe().owner_id, admitted.owner_id)
        self.assertEqual(len(self.rows("session_owners")), 1)
        self.assertEqual(len(self.startup_events()), 1)

    def test_lock_proof_missing_stale_mismatched_ambiguous_and_bounded(self):
        mutations = {
            "no held lock": lambda p: p.info.write_text(f"ino:\t{p.lock.stat().st_ino}\n"),
            "other pid": lambda p: p.info.write_text(p.info.read_text().replace("WRITE 102", "WRITE 103")),
            "shared": lambda p: p.info.write_text(p.info.read_text().replace("WRITE", "READ")),
            "POSIX": lambda p: p.info.write_text(p.info.read_text().replace("FLOCK", "POSIX")),
            "partial": lambda p: p.info.write_text(p.info.read_text().replace("0 EOF", "1 EOF")),
            "finite": lambda p: p.info.write_text(p.info.read_text().replace("0 EOF", "0 99")),
            "wrong inode": lambda p: p.info.write_text(p.info.read_text().replace(f":{p.lock.stat().st_ino}", ":1")),
            "wrong device": lambda p: p.info.write_text(p.info.read_text().replace(
                f"{os.major(p.lock.stat().st_dev):02x}:{os.minor(p.lock.stat().st_dev):02x}:", "ff:ff:")),
            "wrong fdinfo": lambda p: p.info.write_text(p.info.read_text().replace("ino:\t", "ino:\t9")),
            "missing fdinfo": lambda p: p.info.unlink(),
            "missing descriptor": lambda p: (p.root / "102/fd/7").unlink(),
            "deleted lock": lambda p: p.lock.unlink(),
            "second session": lambda p: p.writer(uuid4(), "9"),
            "duplicate descriptor": lambda p: p.writer(p.session, "9"),
            "descriptor bound": lambda p: [(p.root / "102/fd" / str(i)).symlink_to("pipe:[1]") for i in range(10, 266)],
            "oversized fdinfo": lambda p: p.info.write_text(p.info.read_text() + "x" * 65536),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                proc = Proc(self)
                mutate(proc)
                with patch.object(self.app.session_owners, "connect", side_effect=AssertionError("Unexpected admission")) as connect:
                    self.assertEqual((proc.observe().outcome, proc.observe().reason), ("unavailable", "observation_unavailable"))
                    connect.assert_not_called()
        # Named-lock replacement with the original inode still open is refused.
        proc = Proc(self)
        old = proc.lock.with_suffix(".old")
        proc.lock.rename(old)
        proc.lock.symlink_to(old)
        self.assertEqual(proc.observe().outcome, "unavailable")
        self.assertFalse(self.rows("session_owners"))
        self.assertFalse(self.startup_events())

    def test_tree_bounds_missing_facts_and_final_revalidation(self):
        for name in ("cycle", "missing", "reparented", "reused", "other executable", "second codex", "claude root",
                     "nested native", "depth", "count", "missing children"):
            with self.subTest(name=name):
                p = Proc(self)
                if name == "cycle":
                    p.children(102, (100,))
                elif name == "missing":
                    p.children(102, (999,))
                elif name == "missing children":
                    (p.root / "101/task/101/children").unlink()
                elif name == "reparented":
                    path = p.root / "102/stat"
                    path.write_text(path.read_text().replace("S 101", "S 999"))
                elif name == "reused":
                    p.shell = p.shell.model_copy(update={"shell": p.shell.shell.model_copy(update={"start_ticks": 1})})
                elif name == "other executable":
                    (p.root / "102/exe").unlink()
                    (p.root / "102/exe").symlink_to(p.wrapper)
                elif name in {"second codex", "claude root", "nested native"}:
                    parent = 102 if name == "nested native" else 100
                    p.children(parent, (103,) if parent == 102 else (101, 103))
                    p.node(103, parent, p.programs[name == "claude root"].executable)
                elif name == "depth":
                    p.children(102, (103,))
                    for pid in range(103, 134):
                        p.node(pid, pid - 1, p.wrapper, (pid + 1,) if pid < 133 else ())
                elif name == "count":
                    p.children(100, tuple(range(101, 357)))
                    for pid in range(103, 357):
                        p.node(pid, 100, p.wrapper)
                self.assertEqual(p.observe().outcome, "unavailable")
        p = Proc(self)
        self.assertEqual(p.observe(programs=(p.programs[0].model_copy(update={"cli_version": "unknown"}),)).outcome, "unavailable")
        self.assertEqual(p.observe(programs=(p.programs[0].model_copy(update={"provider": "claude", "cli_version": "2.1.287"}),)).outcome,
                         "unavailable")
        calls = []

        def change_after_first_proof():
            calls.append(True)
            if len(calls) == 2:
                p.info.write_text("ino:\t1\n")

        self.assertEqual(p.observe(guard=change_after_first_proof).outcome, "unavailable")
        p = Proc(self)
        original = native_startup._node
        visits = []

        def change_in_second_pass(process):
            result = original(process)
            visits.append(process.pid)
            if visits.count(102) == 2:
                return 999, result[1]
            return result

        with patch.object(native_startup, "_node", side_effect=change_in_second_pass):
            self.assertEqual(p.observe().outcome, "unavailable")
        p = Proc(self)
        with p.patched(), patch.object(self.app.terminals, "native_context",
                return_value=p.shell.model_copy(update={"spec": p.shell.spec.model_copy(update={"environment_generation": 2})})):
            self.assertEqual(self.app.native_setup._startup.observe(Invocation(actor_id=self.host["actor_id"]),
                terminal=p.shell, programs=p.programs, guard=lambda: None).outcome, "unavailable")
        self.assertFalse(self.rows("session_owners"))

    def test_incumbent_retention_and_crash_between_connection_and_event(self):
        p = Proc(self)
        rival = self.candidate()
        incumbent = self.owners.connect(rival)
        self.assertEqual(p.observe().outcome, "conflict")
        self.state(rival, "unknown")
        self.assertEqual(p.observe().outcome, "unavailable")
        p.info.unlink()
        before = self.rows("session_owners")
        self.assertEqual(p.observe().outcome, "unavailable")
        self.assertEqual(self.rows("session_owners"), before)
        self.assertEqual(before[0]["owner_id"], str(incumbent.owner_id))
        self.assertEqual(before[0]["state"], "active")
        self.assertFalse(self.startup_events())
        self.state(rival, "exited")
        p = Proc(self)
        with patch.object(native_startup, "append_event", side_effect=RuntimeError("private crash after connect")):
            self.assertEqual(p.observe().outcome, "unavailable")
        connected = next(row for row in self.rows("session_owners") if row["state"] == "active")
        self.assertEqual(connected["run_generation"], 2)
        self.assertFalse(self.startup_events())
        self.restart()
        recovered = p.observe()
        self.assertEqual((str(recovered.owner_id), recovered.run_generation), (connected["owner_id"], 2))
        self.assertEqual(recovered.outcome, "reconnected")
        self.assertEqual(p.observe().outcome, "observed")
        self.restart()
        self.assertEqual(p.observe().owner_id, recovered.owner_id)
        self.assertEqual(len(self.startup_events()), 1)
        self.assertEqual(len(self.rows("session_owners")), 2)
