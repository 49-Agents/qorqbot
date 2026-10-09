import dataclasses
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import ValidationError

from qorqut_browser.contracts import (
    Binding, BrowserConfig, BrowserError, Click, Command, EffectResult, Fill,
    Identity, Navigate, Reconcile, Screenshot, Snapshot,
)
from qorqut_browser.control import Gate, NotStarted
from qorqut_browser.state import Store, check_paths, exclusive_file, mount_inventory
from qorqut_browser.supervisor import (
    DesktopBinaries, ReadyState, bind_child, check_ready, child_environment,
    desktop_argv, fingerprint, ordered_shutdown, stop_owned, verify_process,
)
from qorqut_browser.tools import AgentBrowser


ACTOR = UUID("baaf15cf-4a93-4357-991d-7b615d03b144")
CTX = multiprocessing.get_context("fork")


def configuration(root):
    agent_id = uuid4()
    root = root / str(agent_id)
    dirs = {key: root / key for key in ("runtime", "home", "profile", "downloads", "artifacts")}
    for path in dirs.values():
        path.mkdir(mode=0o700, parents=True)
    mount = max((m for m in mount_inventory() if root.is_relative_to(m.path)), key=lambda m: len(m.path.parts))
    identity = Identity(agent_id=agent_id, recovery_epoch=uuid4(), environment_generation=1,
                        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                        browser_instance=uuid4(), protocol=1)
    return BrowserConfig(binding=Binding(identity=identity, control_epoch=0), uid=os.getuid(),
        database=dirs["runtime"] / "browser.sqlite3", state_file=dirs["runtime"] / "ready.json",
        home=dirs["home"], profile=dirs["profile"], downloads=dirs["downloads"], artifacts=dirs["artifacts"],
        mounts=(mount,), cdp_host="127.0.0.1", cdp_port=49181, vnc_port=49182, service_port=49183, display=971)


def ready(config):
    check_paths(config)  # fixture: no claim of Chrome, CDP or VNC readiness


def successful(command, fd):
    return EffectResult(text="synthetic result")


def run_command(gate, command, send):
    try:
        send.send(gate.execute(command, actor_id=ACTOR, timeout=15).model_dump(mode="json"))
    except Exception as error:
        send.send({"exception": type(error).__name__, "code": getattr(error, "code", None)})
    finally:
        send.close()


def run_control(gate, request, send):
    try:
        send.send(gate.reconcile(request, actor_id=ACTOR, timeout=15).model_dump(mode="json"))
    except Exception as error:
        send.send({"exception": type(error).__name__, "code": getattr(error, "code", None)})
    finally:
        send.close()


class BrowserFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qorqut-browser-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = configuration(self.root)
        self.store = Store(self.config)
        self.store.initialize()
        self.gate = Gate(self.store, successful, readiness=ready)
        self.gate.startup()
        self.enable()

    def enable(self, gate=None, mode="automation", settled=()):
        gate = gate or self.gate
        current = gate.store.status().binding
        desired = Binding(identity=gate.store.config.binding.identity, control_epoch=current.control_epoch + 1)
        request = Reconcile(command_id=uuid4(), expected=current, desired=desired, mode=mode,
                            settled_commands=settled, evidence="Synthetic fixture application confirmation")
        receipt = gate.reconcile(request, actor_id=ACTOR)
        self.assertEqual(receipt.outcome, "completed")
        return desired

    def command(self, action=None, *, command_id=None):
        return Command(command_id=command_id or uuid4(), binding=self.store.status().binding,
                       action=action or Click(kind="click", selector="e1"))

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(BrowserError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def process(self, target, *args):
        process = CTX.Process(target=target, args=args)
        process.start()
        def cleanup():
            if process.is_alive():
                process.kill()
            process.join(3)
        self.addCleanup(cleanup)
        return process

    def wait_until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("Timed out waiting for an actual fixture boundary")

class GateTest(BrowserFixture):
    def test_committed_completion_releases_lock_retained_by_idle_descendant(self):
        children = []
        def completed(command, fd):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     pass_fds=(fd,), start_new_session=True)
            children.append(child)
            def cleanup():
                child.terminate()
                child.wait(3)
            self.addCleanup(cleanup)
            return EffectResult(text="confirmed completion")
        self.gate.executor = completed
        command = self.command()
        receipt = self.gate.execute(command, actor_id=ACTOR)
        self.assertEqual(receipt.outcome, "completed")
        with self.store.connection() as db:
            self.assertEqual(Store.get_receipt(db, command.command_id), receipt)
        self.assertIsNone(children[0].poll())
        self.gate.executor = successful
        self.assertEqual(self.gate.execute(self.command(), actor_id=ACTOR, timeout=.05).outcome, "completed")
        self.assertIsNone(children[0].poll())

    def test_distinct_bindings_cannot_share_receipts_or_paths(self):
        other = configuration(self.root)
        other_store = Store(other)
        other_store.initialize()
        other_gate = Gate(other_store, successful, readiness=ready)
        other_gate.startup()
        other_binding = self.enable(other_gate)
        command = self.command()
        first = self.gate.execute(command, actor_id=ACTOR)
        second_command = Command(command_id=command.command_id, binding=other_binding, action=command.action)
        second = other_gate.execute(second_command, actor_id=ACTOR)
        self.assertNotEqual(first.binding, second.binding)
        self.assertNotEqual(self.config.profile, other.profile)
        self.assert_code("idempotency_conflict", self.gate.execute, second_command, actor_id=ACTOR)
        shared_profile = other.model_copy(update={"profile": self.config.profile})
        self.assert_code("target_changed", Store(shared_profile).initialize)
        shared_database = other.model_copy(update={"database": self.config.database, "state_file": self.config.state_file})
        self.assert_code("target_changed", Store(shared_database).initialize)

    def test_duplicate_payload_actor_and_binding_conflicts(self):
        calls = []
        self.gate.executor = lambda command, fd: (calls.append(command), EffectResult(text="once"))[1]
        command = self.command()
        receipt = self.gate.execute(command, actor_id=ACTOR)
        self.enable(mode="human")
        self.assertEqual(self.gate.execute(command, actor_id=ACTOR), receipt)
        changed = command.model_copy(update={"action": Click(kind="click", selector="e2")})
        self.assert_code("idempotency_conflict", self.gate.execute, changed, actor_id=ACTOR)
        self.assert_code("idempotency_conflict", self.gate.execute, command, actor_id=uuid4())
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValidationError):
            Command.model_validate({**command.model_dump(), "actor_id": uuid4()})
        with self.assertRaises(ValidationError):
            self.config.profile = self.root

    def test_sqlite_intent_survives_abrupt_exit_and_never_replays(self):
        effect_file = self.root / "effect"
        def crash(command, fd):
            # The effect witness exists only after a separately opened connection
            # observes committed intent. Then simulate death before outcome commit.
            with self.store.connection() as db:
                self.assertEqual(Store.get_receipt(db, command.command_id).outcome, "intent")
            exclusive_file(effect_file, b"clicked once")
            os._exit(86)
        self.gate.executor = crash
        command = self.command()
        receive, send = CTX.Pipe(duplex=False)
        process = self.process(run_command, self.gate, command, send)
        process.join(3)
        self.assertEqual(process.exitcode, 86)
        self.assertEqual(effect_file.read_bytes(), b"clicked once")
        restarted = Gate(Store(self.config), lambda *_: self.fail("Uncertain effect replayed"), readiness=ready)
        restarted.startup()
        receipt = restarted.execute(command, actor_id=ACTOR)
        self.assertEqual(receipt.outcome, "uncertain")
        self.assert_code("idempotency_conflict", restarted.execute,
                         command.model_copy(update={"action": Navigate(kind="navigate", url="about:blank")}), actor_id=ACTOR)
        current = self.store.status().binding
        request = Reconcile(command_id=uuid4(), expected=current,
                            desired=Binding(identity=current.identity, control_epoch=current.control_epoch + 1),
                            mode="human", evidence="Unknown effects cannot count as settled")
        self.assertEqual(restarted.reconcile(request, actor_id=ACTOR).outcome, "pending")
        self.assertFalse(self.store.status().ready)
        self.enable(restarted, settled=(command.command_id,))
        self.assertEqual(restarted.execute(command, actor_id=ACTOR), receipt)
        self.assertEqual(effect_file.read_bytes(), b"clicked once")
        receive.close()
        send.close()

    def test_crash_immediately_after_intent_is_also_uncertain(self):
        self.gate.executor = lambda *_: os._exit(87)
        command = self.command()
        receive, send = CTX.Pipe(duplex=False)
        process = self.process(run_command, self.gate, command, send)
        process.join(3)
        self.assertEqual(process.exitcode, 87)
        restarted = Gate(self.store, successful, readiness=ready)
        restarted.startup()
        self.assertEqual(restarted.execute(command, actor_id=ACTOR).outcome, "uncertain")
        receive.close()
        send.close()

    def test_human_transition_closes_admission_then_waits_for_process(self):
        started, release = CTX.Event(), CTX.Event()
        def held(command, fd):
            started.set()
            if not release.wait(20):
                raise TimeoutError()
            return EffectResult(text="settled")
        self.gate.executor = held
        command = self.command()
        output, send = CTX.Pipe(duplex=False)
        process = self.process(run_command, self.gate, command, send)
        self.assertTrue(started.wait(2))
        current = self.store.status().binding
        request = Reconcile(command_id=uuid4(), expected=current,
                            desired=Binding(identity=current.identity, control_epoch=current.control_epoch + 1),
                            mode="human", evidence="Take human control after the held command")
        control_output, control_send = CTX.Pipe(duplex=False)
        control = self.process(run_control, self.gate, request, control_send)
        self.wait_until(lambda: self.store.status().mode == "yielding")
        self.assertFalse(self.store.status().ready)
        self.assertFalse(control_output.poll(.1))
        self.assert_code("not_ready", self.gate.execute, self.command(), actor_id=ACTOR, timeout=.05)
        release.set()
        self.assertTrue(output.poll(2))
        self.assertEqual(output.recv()["outcome"], "completed")
        self.assertTrue(control_output.poll(2))
        self.assertEqual(control_output.recv()["outcome"], "completed")
        process.join(2)
        control.join(2)
        self.assertTrue(self.store.status().ready)
        self.assertEqual(self.store.status().mode, "human")
        for pipe in (output, send, control_output, control_send):
            pipe.close()

    def test_control_timeout_stays_pending_and_retry_settles_once(self):
        started, release = CTX.Event(), CTX.Event()
        def held(command, fd):
            started.set()
            release.wait(20)
            return EffectResult()
        self.gate.executor = held
        output, send = CTX.Pipe(duplex=False)
        process = self.process(run_command, self.gate, self.command(), send)
        self.assertTrue(started.wait(2))
        current = self.store.status().binding
        request = Reconcile(command_id=uuid4(), expected=current,
                            desired=Binding(identity=current.identity, control_epoch=current.control_epoch + 1),
                            mode="human", evidence="Timeout fixture")
        self.assertEqual(self.gate.reconcile(request, actor_id=ACTOR, timeout=.02).outcome, "pending")
        self.assertEqual(self.store.status().mode, "yielding")
        release.set()
        process.join(2)
        done = self.gate.reconcile(request, actor_id=ACTOR)
        self.assertEqual(done.outcome, "completed")
        self.assertEqual(self.gate.reconcile(request, actor_id=ACTOR), done)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM commands WHERE command_id=?", (str(request.command_id),)).fetchone()[0], 1)
        output.close()
        send.close()

    def test_concurrent_control_retry_has_one_confirmation_event(self):
        started, release = CTX.Event(), CTX.Event()
        def slow_readiness(config):
            ready(config)
            started.set()
            release.wait(20)
        self.gate.readiness = slow_readiness
        current = self.store.status().binding
        request = Reconcile(command_id=uuid4(), expected=current,
                            desired=Binding(identity=current.identity, control_epoch=current.control_epoch + 1),
                            mode="human", evidence="Concurrent retry of the same control transition")
        outputs, sends, processes = [], [], []
        for _ in range(2):
            output, send = CTX.Pipe(duplex=False)
            outputs.append(output)
            sends.append(send)
            processes.append(self.process(run_control, self.gate, request, send))
        self.assertTrue(started.wait(2))
        release.set()
        for process in processes:
            process.join(3)
        self.assertEqual(outputs[0].recv(), outputs[1].recv())
        with self.store.connection() as db:
            events = db.execute("SELECT current FROM control_events WHERE command_id=?", (str(request.command_id),)).fetchall()
        self.assertEqual(sum(json.loads(row[0])["ready"] for row in events), 1)
        for pipe in outputs + sends:
            pipe.close()

    def test_process_lock_serializes_concurrent_duplicate_and_other_commands(self):
        started, release = CTX.Event(), CTX.Event()
        effect_file = self.root / "only-once"
        def held(command, fd):
            exclusive_file(effect_file, b"one")
            started.set()
            release.wait(20)
            return EffectResult(text="once")
        self.gate.executor = held
        command = self.command()
        output, send = CTX.Pipe(duplex=False)
        first = self.process(run_command, self.gate, command, send)
        self.assertTrue(started.wait(2))
        second_output, second_send = CTX.Pipe(duplex=False)
        second = self.process(run_command, self.gate, command, second_send)
        self.assertFalse(second_output.poll(.1))
        self.assert_code("not_ready", self.gate.execute, self.command(), actor_id=ACTOR, timeout=.02)
        release.set()
        first.join(2)
        second.join(2)
        self.assertEqual(output.recv(), second_output.recv())
        self.assertEqual(effect_file.read_bytes(), b"one")
        for pipe in (output, send, second_output, second_send):
            pipe.close()

    def test_saved_human_epoch_stale_service_and_explicit_instance_rebind(self):
        binding = self.enable(mode="human")
        restarted = Gate(Store(self.config), successful, readiness=ready)
        restarted.startup()
        status = self.store.status()
        self.assertEqual((status.mode, status.binding, status.ready), ("human", binding, False))
        self.assert_code("not_ready", self.gate.execute, self.command(), actor_id=ACTOR)
        self.assert_code("not_ready", restarted.execute, self.command(), actor_id=ACTOR)
        self.enable(restarted)
        stale = Command(command_id=uuid4(), binding=binding, action=Snapshot(kind="snapshot"))
        self.assert_code("target_changed", restarted.execute, stale, actor_id=ACTOR)
        new_identity = self.config.binding.identity.model_copy(update={"browser_instance": uuid4()})
        replacement_config = self.config.model_copy(update={"binding": Binding(identity=new_identity, control_epoch=0)})
        replacement = Gate(Store(replacement_config), successful, readiness=ready)
        replacement.startup()
        self.assert_code("target_changed", replacement.execute, self.command(), actor_id=ACTOR)
        self.enable(replacement)
        self.assertEqual(self.store.status().binding.identity.browser_instance, new_identity.browser_instance)

    def test_optional_sidecars_can_disappear_during_observation(self):
        lstat = Path.lstat
        # Keep real SQLite sidecars present before the disappearing-leaf seam.
        with self.store.connection() as db:
            expected = Store.status_in(db)
            for suffix in ("-wal", "-shm"):
                with self.subTest(suffix=suffix):
                    sidecar = Path(str(self.config.database) + suffix)
                    self.assertTrue(sidecar.exists())
                    observed = []

                    def disappearing(path):
                        if path == sidecar:
                            observed.append(path)
                            raise FileNotFoundError(path)
                        return lstat(path)

                    with patch.object(Path, "lstat", disappearing):
                        self.assertEqual(self.store.status(), expected)
                    self.assertEqual(observed, [sidecar])

    def test_missing_state_profile_mount_and_readiness_fail_closed(self):
        self.assert_code("not_ready", check_ready, self.config)
        default_gate = Gate(self.store, lambda *_: self.fail("Called without readiness"))
        default_gate.startup()
        current = self.store.status().binding
        request = Reconcile(command_id=uuid4(), expected=current,
                            desired=Binding(identity=current.identity, control_epoch=current.control_epoch + 1),
                            mode="human", evidence="Missing participant cannot be quiescent")
        self.assertEqual(default_gate.reconcile(request, actor_id=ACTOR).outcome, "pending")
        self.assertFalse(self.store.status().ready)
        moved = self.config.profile.with_name("retained-profile")
        self.config.profile.rename(moved)
        self.assert_code("not_ready", check_paths, self.config)
        self.assertFalse(self.config.profile.exists())
        moved.rename(self.config.profile)
        absent_mount = self.config.mounts[0].model_copy(update={"mount_id": 999999999})
        self.assert_code("not_ready", check_paths, self.config.model_copy(update={"mounts": (absent_mount,)}))
        self.config.database.unlink()
        self.assert_code("not_ready", self.store.status)
        self.assertFalse(self.config.database.exists())

    def test_unexpected_error_closes_admission_but_known_pre_effect_failure_does_not(self):
        command = self.command()
        def no_launch(*args):
            raise NotStarted("not_ready", "No subprocess created")
        self.gate.executor = no_launch
        self.assertEqual(self.gate.execute(command, actor_id=ACTOR).outcome, "failed")
        self.assertTrue(self.store.status().ready)
        def uncertain(*args):
            raise RuntimeError("possible click; private page text must not be persisted")
        self.gate.executor = uncertain
        receipt = self.gate.execute(self.command(), actor_id=ACTOR)
        self.assertEqual(receipt.outcome, "uncertain")
        self.assertEqual(receipt.error, "uncertain_effect")
        self.assertFalse(self.store.status().ready)


FAKE_HELPER = r'''
import json, os, pathlib, sys, time
if sys.argv[1:] == ["--version"]:
    print("agent-browser 0.38.1")
    raise SystemExit
args = sys.argv[1:]
assert args[0] == "--config" and pathlib.Path(args[1]).read_text() == "{}\n"
assert args[2] == "--session" and args[4] == "--cdp"
pathlib.Path("seen.json").write_text(json.dumps({"argv": args, "env": dict(os.environ), "pid": os.getpid()}))
action = args[6:]
if action[0] == "snapshot":
    print("D" * 20000)
elif action[0] == "screenshot":
    pathlib.Path(action[1]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic screenshot")
    print("screenshot saved")
elif action[0] == "fill" and action[2] == "HOLD":
    while not pathlib.Path("release").exists():
        time.sleep(.01)
    print("settled")
elif action[0] == "fill" and action[2] == "FAIL":
    raise SystemExit(7)
else:
    print("ok")
'''


class HelperTest(BrowserFixture):
    def setUp(self):
        super().setUp()
        self.helper = self.root / "fake-agent-browser"
        self.helper.write_text(f"#!{sys.executable}\n" + FAKE_HELPER)
        self.helper.chmod(0o700)
        self.gate.executor = AgentBrowser(self.config, self.helper, timeout=2, readiness=ready)

    def test_real_subprocess_fixed_argv_environment_and_bounded_artifacts(self):
        # Ambient personal/target overrides never reach the actual subprocess.
        from unittest.mock import patch
        with patch.dict(os.environ, {"AGENT_BROWSER_PROFILE": "/not-owned", "HOME": "/not-owned", "DISPLAY": ":0"}):
            actions = [Navigate(kind="navigate", url="https://example.invalid/a?x=$(touch nope)"),
                       Click(kind="click", selector="e12"),
                       Fill(kind="fill", selector="e13", text="literal ' ; $(touch nope)"),
                       Snapshot(kind="snapshot", depth=3, interactive=True),
                       Screenshot(kind="screenshot", full_page=True)]
            receipts = []
            for action in actions:
                command = self.command(action)
                receipt = self.gate.execute(command, actor_id=ACTOR)
                self.assertEqual(receipt.outcome, "completed")
                seen = json.loads((self.config.artifacts / str(command.command_id) / "seen.json").read_text())
                self.assertEqual(seen["argv"][4:6], ["--cdp", self.config.cdp_endpoint])
                self.assertEqual(seen["env"]["HOME"], str(self.config.home))
                self.assertEqual(seen["env"]["DISPLAY"], f":{self.config.display}")
                self.assertFalse(any(k.startswith("AGENT_BROWSER_") for k in seen["env"]))
                if isinstance(action, Fill):
                    self.assertEqual(seen["argv"][6:], ["fill", "@e13", action.text])
                receipts.append(receipt)
            self.assertLess(len(receipts[3].model_dump_json()), 3000)
            self.assertEqual(receipts[3].result.text, "")
            self.assertEqual(receipts[3].result.artifacts[0].size, 20001)
            self.assertEqual(receipts[4].result.artifacts[0].media_type, "image/png")
            for receipt in receipts:
                for artifact in receipt.result.artifacts:
                    self.assertTrue(artifact.path.is_relative_to(self.config.artifacts))
                    self.assertEqual(hashlib.sha256(artifact.path.read_bytes()).hexdigest(), artifact.sha256)
            self.assertFalse((self.config.artifacts / "nope").exists())

    def test_payload_cannot_supply_helper_session_profile_or_screenshot_path(self):
        for payload in [dict(kind="click", selector="--cdp=9222"),
                        dict(kind="fill", selector="e1", text="--profile=/tmp/wrong"),
                        dict(kind="navigate", url="--session=wrong"),
                        dict(kind="screenshot", path="/tmp/wrong.png"),
                        dict(kind="snapshot", session="wrong")]:
            with self.assertRaises(ValidationError):
                Command.model_validate(dict(command_id=uuid4(), binding=self.store.status().binding, action=payload))

    def test_helper_error_and_timeout_are_uncertain_and_never_repeated(self):
        for value in ("FAIL", "HOLD"):
            command = self.command(Fill(kind="fill", selector="e1", text=value))
            self.gate.executor = AgentBrowser(self.config, self.helper, timeout=.2, readiness=ready)
            receipt = self.gate.execute(command, actor_id=ACTOR)
            self.assertEqual(receipt.outcome, "uncertain")
            self.assertFalse(self.store.status().ready)
            self.assertEqual(self.gate.execute(command, actor_id=ACTOR), receipt)
            self.enable(settled=(command.command_id,))

    def test_output_overflow_and_pre_dispatch_readiness_loss(self):
        command = self.command(Snapshot(kind="snapshot"))
        self.gate.executor = AgentBrowser(self.config, self.helper, artifact_limit=8192, readiness=ready)
        self.assertEqual(self.gate.execute(command, actor_id=ACTOR).outcome, "uncertain")
        output = self.config.artifacts / str(command.command_id) / "command.stdout"
        self.assertLessEqual(output.stat().st_size, 8192)
        self.enable(settled=(command.command_id,))
        def unavailable(config):
            raise BrowserError("not_ready", "Target disappeared during helper version probe")
        command = self.command()
        self.gate.executor = AgentBrowser(self.config, self.helper, readiness=unavailable)
        self.assertEqual(self.gate.execute(command, actor_id=ACTOR).outcome, "failed")
        directory = self.config.artifacts / str(command.command_id)
        self.assertTrue((directory / "version.stdout").exists())
        self.assertFalse((directory / "seen.json").exists())

    def test_orphan_helper_retains_process_lock_after_service_is_killed(self):
        self.gate.executor = AgentBrowser(self.config, self.helper, timeout=5, readiness=ready)
        command = self.command(Fill(kind="fill", selector="e1", text="HOLD"))
        output, send = CTX.Pipe(duplex=False)
        process = self.process(run_command, self.gate, command, send)
        directory = self.config.artifacts / str(command.command_id)
        self.wait_until(lambda: (directory / "seen.json").exists())
        pid = json.loads((directory / "seen.json").read_text())["pid"]
        observed = fingerprint(pid)
        def cleanup_helper():
            (directory / "release").touch(exist_ok=True)
            try:
                verify_process(observed)
                os.kill(pid, signal.SIGKILL)
            except (OSError, BrowserError):
                pass
        self.addCleanup(cleanup_helper)
        process.kill()
        process.join(2)
        restarted = Gate(Store(self.config), successful, readiness=ready)
        restarted.startup(timeout=.01)
        current = self.store.status().binding
        request = Reconcile(command_id=uuid4(), expected=current,
                            desired=Binding(identity=current.identity, control_epoch=current.control_epoch + 1),
                            mode="human", settled_commands=(command.command_id,), evidence="Wait for actual inherited lock")
        self.assertEqual(restarted.reconcile(request, actor_id=ACTOR, timeout=.02).outcome, "pending")
        self.assertFalse(self.store.status().ready)
        (directory / "release").touch()
        self.wait_until(lambda: self._helper_exited(observed))
        self.assertEqual(restarted.reconcile(request, actor_id=ACTOR).outcome, "completed")
        self.assertEqual(restarted.execute(command, actor_id=ACTOR).outcome, "uncertain")
        output.close()
        send.close()

    @staticmethod
    def _helper_exited(observed):
        try:
            verify_process(observed)
            return False
        except BrowserError:
            return True


class SupervisorTest(BrowserFixture):
    def test_readiness_checks_real_listener_ownership_and_exact_state_binding(self):
        sockets = []
        processes = []
        for role in ("chrome", "display"):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            sockets.append(listener)
            self.addCleanup(listener.close)
        config = self.config.model_copy(update={"cdp_port": sockets[0].getsockname()[1],
                                                "vnc_port": sockets[1].getsockname()[1]})
        body_path = self.root / "cdp-body"
        body_path.write_text(json.dumps({"webSocketDebuggerUrl":
            f"ws://127.0.0.1:{config.cdp_port}/devtools/browser/synthetic"}))
        script = r'''
import os,pathlib,socket,sys
listener=socket.socket(fileno=int(sys.argv[1]))
pathlib.Path(sys.argv[2]).touch()
while True:
    connection,_=listener.accept()
    with connection:
        connection.recv(4096)
        body=pathlib.Path(sys.argv[3]).read_bytes()
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
'''
        for role, listener in zip(("chrome", "display"), sockets):
            started = self.root / f"{role}-listener-started"
            process = subprocess.Popen([sys.executable, "-c", script, str(listener.fileno()), str(started), str(body_path)],
                                       pass_fds=(listener.fileno(),), start_new_session=True)
            processes.append(process)
            def cleanup(process=process):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
            self.addCleanup(cleanup)
            self.wait_until(started.exists)
            listener.close()
        saved = ReadyState(identity=config.binding.identity, resource_hash=config.resource_hash,
                           chrome=fingerprint(processes[0].pid), display=fingerprint(processes[1].pid))
        exclusive_file(config.state_file, saved.model_dump_json().encode())
        check_ready(config)
        changed = saved.model_copy(update={"identity": saved.identity.model_copy(update={"browser_instance": uuid4()})})
        config.state_file.write_text(changed.model_dump_json())
        self.assert_code("target_changed", check_ready, config)
        config.state_file.write_text(saved.model_dump_json())
        body_path.write_text(json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/wrong"}))
        self.assert_code("not_ready", check_ready, config)
        body_path.write_text(json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{config.cdp_port}/devtools/browser/synthetic"}))
        processes[0].kill()
        processes[0].wait()
        self.assert_code("not_ready", check_ready, config)

    def child(self, role, *, ignore_term=False):
        script = ("import signal,time,pathlib,sys; "
                  "signal.signal(signal.SIGTERM, " +
                  ("signal.SIG_IGN" if ignore_term else
                   "lambda *_: (pathlib.Path(sys.argv[1]).open('a').write(sys.argv[2]+'\\n'), sys.exit(0))") +
                  "); pathlib.Path(sys.argv[3]).touch(); time.sleep(30)")
        started = self.root / f"{role}-started"
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root / "shutdown-order"), role, str(started)],
                                   start_new_session=True, env=child_environment(self.config))
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        self.addCleanup(cleanup)
        self.wait_until(started.exists)
        return bind_child(process, self.config, role)

    def test_real_owned_process_shutdown_order_and_fingerprint_checks(self):
        chrome, wm, display = (self.child(role) for role in ("chrome", "window_manager", "display"))
        stale = chrome.fingerprint.model_copy(update={"start_ticks": chrome.fingerprint.start_ticks + 1})
        self.assert_code("not_ready", verify_process, stale)
        self.assert_code("not_ready", stop_owned, dataclasses.replace(chrome, fingerprint=stale), self.config)
        self.assertIsNone(chrome.process.poll())
        ordered_shutdown(self.config, chrome=chrome, window_manager=wm, display=display, timeout=2)
        self.assertEqual((self.root / "shutdown-order").read_text().splitlines(), ["chrome", "window_manager", "display"])
        self.assert_code("not_ready", stop_owned, chrome, self.config)

    def test_unsettled_chrome_keeps_display_and_reaped_identity_is_not_signalled(self):
        chrome = self.child("chrome", ignore_term=True)
        wm, display = (self.child(role) for role in ("window_manager", "display"))
        self.assert_code("not_ready", ordered_shutdown, self.config, chrome=chrome,
                         window_manager=wm, display=display, timeout=.05)
        self.assertIsNone(display.process.poll())
        self.assertIsNone(wm.process.poll())
        chrome.process.kill()
        chrome.process.wait()
        self.assert_code("not_ready", ordered_shutdown, self.config, chrome=chrome,
                         window_manager=wm, display=display, timeout=.05)
        self.assertIsNone(display.process.poll())

    def test_start_argv_requires_existing_profile_xauthority_and_keeps_locks(self):
        xauth = self.config.home / "xauthority"
        exclusive_file(xauth, b"synthetic")
        binaries = DesktopBinaries(*(Path(sys.executable) for _ in range(3)))
        commands = desktop_argv(self.config, binaries, xauthority=xauth)
        self.assertEqual(commands["display"][commands["display"].index("-SecurityTypes") + 1], "None")
        self.assertNotIn("-PasswordFile", commands["display"])
        self.assertIn("-auth", commands["display"])
        self.assertEqual(child_environment(self.config, xauthority=xauth)["XAUTHORITY"], str(xauth))
        self.assertIn(f"--user-data-dir={self.config.profile}", commands["chrome"])
        self.assertIn("--remote-debugging-address=127.0.0.1", commands["chrome"])
        self.assertNotIn("--no-sandbox", commands["chrome"])
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            claimed = self.config.model_copy(update={"cdp_port": listener.getsockname()[1]})
            self.assert_code("not_ready", desktop_argv, claimed, binaries, xauthority=xauth)
        lock = self.config.profile / "SingletonLock"
        lock.symlink_to("unverified-host-999999")
        self.assert_code("not_ready", desktop_argv, self.config, binaries, xauthority=xauth)
        self.assertTrue(lock.is_symlink())
        self.assertEqual(os.readlink(lock), "unverified-host-999999")


if __name__ == "__main__":
    unittest.main()
