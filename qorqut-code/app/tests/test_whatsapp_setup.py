"""WhatsApp setup without a terminal: the dormant state, linking, finishing by itself after the scan, the tool's access rule,
the owner routes. A fake helper process and a fake connector client stand in for WhatsApp; nothing contacts it."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

# The extension is bundled source beside the host (qorqut-code/extensions/whatsapp); it is not installed in the test environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "extensions" / "whatsapp"))

from qorqut_whatsapp import binding, routes, setup  # noqa: E402
from qorqut_whatsapp.extension import WhatsApp, create, status as reader_status  # noqa: E402
from qorqut_whatsapp.operations import NOT_SET_UP, WhatsAppSendOperations  # noqa: E402
from qorqut_whatsapp.receiver import Receiver  # noqa: E402

AGENT = "6f0d8a52-3f4e-4f43-9a0e-2a1f6c1c9b01"
OTHER = "0b9a3c1e-5d2f-4a7b-8c6d-1e2f3a4b5c6d"
REF = "ab" * 32


class FakeProcess:
    def __init__(self, args):
        self.args, self.code, self.done, self.terminated = list(args), None, threading.Event(), False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if not self.done.wait(10 if timeout is None else timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.code

    def exit(self, code):
        self.code = code
        self.done.set()

    def terminate(self):
        self.terminated = True
        self.exit(1)

    kill = terminate


class FakeClient:
    failures = 0

    def __init__(self, config):
        self.config = config

    def account(self):
        if FakeClient.failures:
            FakeClient.failures -= 1
            raise DomainError("operation_unavailable", "The personal WhatsApp connector is unavailable")
        return {"account_ref": REF, "connected": True}


class FakeHost:
    def __init__(self):
        self.workers, self.grants, self.restarts, self.ready = 0, [], [], True

    def coordinator(self):
        return AGENT

    def register_worker(self):
        self.workers += 1

    def grant(self, agent_id):
        self.grants.append(agent_id)
        return True

    def send_ready(self, agent_id):
        return self.ready == "always" or (self.ready is True and self.workers > 0)

    def restart(self, agent_id, text, key):
        self.restarts.append((agent_id, text, key))


def wait_until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not reached")


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve() / "instance"
        self.root.mkdir(mode=0o700)
        helper = Path(folder.name).resolve() / "helper" / binding.NAME
        helper.parent.mkdir()
        helper.write_bytes(b"#!/bin/sh\nexit 0\n")
        helper.chmod(0o755)
        self.helper = helper
        # Temporary folders can be deeper than a Unix socket path allows; the limit itself is tested on its own.
        for patch in (mock.patch.object(binding, "bundled_helper", lambda: self.helper),
                      mock.patch.object(binding, "socket_limit", lambda: 4096)):
            patch.start()
            self.addCleanup(patch.stop)
        FakeClient.failures = 0
        self.processes = []
        self.paths = NS(instance_root=self.root)
        self.receiver = Receiver(self.paths, popen=self.popen)
        self.host = FakeHost()
        self.setup = self.make_setup()
        self.addCleanup(self.setup.stop, 1)

    def make_setup(self):
        service = setup.Setup(self.paths, self.receiver, self.host, popen=self.popen, client_factory=FakeClient)
        service.RETRY_SECONDS, service.ACCOUNT_SECONDS = 0.01, 2.0
        return service

    def popen(self, args, **options):
        process = FakeProcess(args)
        process.options = options
        self.processes.append(process)
        return process

    def pairings(self):
        return [process for process in self.processes if "--pair-only" in process.args]

    def receivers(self):
        return [process for process in self.processes if "--pair-only" not in process.args]

    def data(self):
        for folder in (self.root / "private", binding.data_path(self.root)):
            folder.mkdir(exist_ok=True, mode=0o700)
        return binding.data_path(self.root)

    def link_phone(self):
        data = self.data()
        with sqlite3.connect(data / "session.sqlite3") as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS whatsmeow_device (jid TEXT PRIMARY KEY)")
            connection.execute("INSERT OR IGNORE INTO whatsmeow_device VALUES ('device')")

    def binding_value(self):
        path = binding.root_binding(self.root)
        return json.loads(path.read_text()) if path.exists() else None

    def hand_binding(self, version):
        from qorqut_whatsapp.__main__ import install_reader
        value = install_reader(self.root, AGENT)
        if version == 2:
            value = {**value, "version": 2, "managed_send": {"socket": str(binding.send_socket_path(self.root)),
                     "secret_file": str(binding.send_secret_path(self.root)), "account_ref": "cd" * 32}}
            path = binding.root_binding(self.root)
            path.unlink()
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        return value


class DormantTest(Base):
    def test_with_no_binding_nothing_runs_and_nothing_is_offered(self):
        self.setup.start()
        self.assertEqual(self.processes, [])
        self.assertEqual(self.setup.status(), {"state": "not_set_up", "note": setup.NOTES["not_set_up"], "qr": False,
                                               "send_ready": False})
        self.assertEqual(reader_status(self.paths, AGENT), {"prepared": False, "reason": "not_bound"})
        self.assertIsNone(self.setup.qr())
        self.assertEqual(self.setup.cancel()["state"], "not_set_up")
        self.setup.stop(1)
        self.assertFalse(self.setup.running())

    def test_without_a_bundled_helper_it_is_unsupported_and_start_does_nothing(self):
        with mock.patch.object(binding, "bundled_helper", lambda: self.root / "missing"):
            self.assertEqual(self.setup.status()["state"], "unsupported")
            self.assertEqual(self.setup.begin(AGENT)["state"], "unsupported")
        self.assertEqual(self.processes, [])
        self.assertIsNone(self.binding_value())

    def test_a_store_its_helper_holds_locked_still_reads_as_linked(self):
        self.link_phone()
        with mock.patch.object(binding.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertTrue(binding.linked(self.root))
        with mock.patch.object(binding.sqlite3, "connect", side_effect=sqlite3.OperationalError("no such table: whatsmeow_device")):
            self.assertFalse(binding.linked(self.root))

    def test_a_binding_without_a_linked_phone_starts_no_helper(self):
        self.hand_binding(1)
        self.setup.start()
        self.assertEqual(self.processes, [])
        self.assertEqual(reader_status(self.paths, AGENT), {"prepared": False, "reason": "not_linked"})
        self.assertEqual(self.setup.status()["state"], "not_set_up")
        # An empty session store (a pairing that never finished) is not linked either.
        sqlite3.connect(self.data() / "session.sqlite3").close()
        self.receiver.start()
        self.assertEqual(self.processes, [])

    def test_sending_refuses_with_the_fixed_not_set_up_text(self):
        operations = WhatsAppSendOperations(NS(current_context=lambda ctx, uow: ctx), self.paths, NS())
        agent = NS(principal_kind="agent", delegation_assignment_id=None, project_id=None, principal_id=AGENT)
        with self.assertRaises(DomainError) as caught:
            operations._access(agent, NS())
        self.assertEqual((caught.exception.code, str(caught.exception)), ("operation_unavailable", NOT_SET_UP))

    def test_the_socket_limit_is_the_platforms(self):
        mock.patch.stopall()
        try:
            with mock.patch.object(sys, "platform", "darwin"):
                self.assertEqual(binding.socket_limit(), 104)
                self.assertFalse(binding.socket_fits("/" + "a" * 103))
                self.assertTrue(binding.socket_fits("/" + "a" * 102))
            with mock.patch.object(sys, "platform", "linux"):
                self.assertEqual(binding.socket_limit(), 108)
                self.assertTrue(binding.socket_fits("/" + "a" * 106))
        finally:
            for patch in (mock.patch.object(binding, "bundled_helper", lambda: self.helper),
                          mock.patch.object(binding, "socket_limit", lambda: 4096)):
                patch.start()


class LinkingTest(Base):
    def test_start_binds_the_reader_and_pairs_as_a_child_of_the_service(self):
        view = self.setup.begin(AGENT)
        self.assertEqual(view["state"], "linking")
        value = self.binding_value()
        self.assertEqual((value["version"], value["agent_id"]), (1, AGENT))
        self.assertEqual(stat.S_IMODE(binding.root_binding(self.root).stat().st_mode), 0o600)
        self.assertTrue(binding.setup_marker_path(self.root).is_file())
        [pairing] = self.pairings()
        self.assertEqual(pairing.args[1:], ["serve", "--data-dir", str(binding.data_path(self.root)), "--socket",
                                            value["socket"], "--pair", "--pair-only"])
        self.assertNotIn("--managed-send-socket", pairing.args)
        # The pairing log names the code image, so it is kept beside it in the private data folder.
        self.assertTrue((binding.data_path(self.root) / "pair.log").is_file())
        self.assertFalse((self.root / "logs").exists())
        self.assertTrue(pairing.options["start_new_session"])
        # Idempotent while linking.
        self.assertEqual(self.setup.begin(AGENT)["state"], "linking")
        self.assertEqual(len(self.pairings()), 1)
        self.assertEqual(self.setup.status()["qr"], False)
        (binding.data_path(self.root) / "pair.png").write_bytes(b"\x89PNG-made-up")
        self.assertEqual(self.setup.status()["qr"], True)
        self.assertEqual(self.setup.qr(), b"\x89PNG-made-up")

    def test_the_tool_result_never_carries_the_code_or_its_path(self):
        tool = setup.SetupTool(NS(database=None), self.setup)
        tool.caller = lambda ctx, uow: AGENT
        tool.app = NS(database=NS(read=lambda: _Unit()))
        (self.data() / "pair.png").write_bytes(b"\x89PNG-made-up")
        plan = NS(arguments={"action": "start"}, payload_hash="h1")
        tool.external(NS(), plan)
        outcome = tool.apply(NS(), plan, None)
        self.assertEqual(outcome["state"], "linking")
        self.assertIn("Settings > Connections > WhatsApp", outcome["next"])
        self.assertIn("Linked devices > Link a device", outcome["next"])
        text = json.dumps(outcome)
        for hidden in ("pair.png", str(self.root), "PNG"):
            self.assertNotIn(hidden, text)

    def test_an_expired_code_fails_and_a_later_start_is_clean(self):
        self.setup.begin(AGENT)
        [pairing] = self.pairings()
        (binding.data_path(self.root) / "pair.log").write_bytes(b"pairing ended (timeout); restart the operator pairing command\n")
        pairing.exit(1)
        wait_until(lambda: self.setup.status()["state"] == "failed")
        self.assertEqual(self.setup.status()["note"], setup.NOTES["expired"])
        self.assertIsNone(self.binding_value())
        self.assertFalse(binding.setup_marker_path(self.root).exists())
        self.assertEqual(self.setup.begin(AGENT)["state"], "linking")
        self.assertEqual(len(self.pairings()), 2)

    def test_another_failure_says_linking_did_not_finish(self):
        self.setup.begin(AGENT)
        self.pairings()[0].exit(1)
        wait_until(lambda: self.setup.status()["state"] == "failed")
        self.assertEqual(self.setup.status()["note"], setup.NOTES["pair_failed"])

    def test_cancel_stops_the_pairing_and_removes_the_never_linked_binding(self):
        self.setup.begin(AGENT)
        view = self.setup.cancel()
        self.assertTrue(self.pairings()[0].terminated)
        self.assertEqual(view["state"], "not_set_up")
        self.assertIsNone(self.binding_value())
        self.assertFalse(binding.setup_marker_path(self.root).exists())

    def test_stopping_the_service_ends_a_pairing(self):
        self.setup.begin(AGENT)
        self.setup.stop(1)
        self.assertTrue(self.pairings()[0].terminated)
        self.assertFalse(self.setup.running())
        self.assertEqual(self.make_setup().status()["state"], "not_set_up")


class FinishingTest(Base):
    def scan(self):
        self.setup.begin(AGENT)
        self.link_phone()
        self.pairings()[0].exit(0)
        wait_until(lambda: not binding.setup_marker_path(self.root).exists())

    def assert_finished(self):
        value = self.binding_value()
        binding.validate_binding(value, self.root)
        self.assertEqual((value["version"], value["agent_id"], value["managed_send"]["account_ref"]), (2, AGENT, REF))
        secret = binding.send_secret_path(self.root)
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertEqual(len(secret.read_bytes()), 64)
        receiver = self.receivers()[-1]
        self.assertEqual(receiver.args[receiver.args.index("--managed-send-socket") + 1], value["managed_send"]["socket"])
        self.assertEqual(receiver.args[receiver.args.index("--managed-send-secret-file") + 1], str(secret))
        self.assertTrue(self.receiver.running() and self.receiver.managed)
        self.assertGreaterEqual(self.host.workers, 1)
        self.assertEqual(self.host.grants[-1], AGENT)
        self.assertEqual(self.host.restarts[-1][:2], (AGENT, setup.LINKED_NOTICE))
        self.assertNotIn(secret.read_text(), json.dumps(value))
        self.assertEqual(self.setup.status(), {"state": "connected", "note": setup.NOTES["connected"], "qr": False,
                                               "send_ready": True})

    def test_after_the_scan_everything_finishes_by_itself(self):
        self.scan()
        self.assert_finished()
        self.assertEqual(len(self.pairings()), 1)
        self.assertEqual(reader_status(self.paths, AGENT), {"prepared": True})
        self.assertEqual(reader_status(self.paths, OTHER), {"prepared": False, "reason": "not_bound"})

    def test_a_connector_that_answers_late_is_waited_for(self):
        FakeClient.failures = 3
        self.scan()
        self.assert_finished()

    def test_a_restart_after_the_scan_finishes_on_the_next_start(self):
        self.hand_binding(1)
        self.setup._mark(AGENT)
        self.link_phone()
        self.setup.start()
        wait_until(lambda: not binding.setup_marker_path(self.root).exists())
        self.assert_finished()
        self.assertEqual(self.pairings(), [])

    def test_a_resumed_setup_also_finishes_on_status(self):
        self.hand_binding(1)
        self.setup._mark(AGENT)
        self.link_phone()
        self.assertEqual(self.setup.status()["state"], "finishing")
        wait_until(lambda: not binding.setup_marker_path(self.root).exists())
        self.assert_finished()

    def test_a_step_that_keeps_failing_is_reported_and_start_retries(self):
        FakeClient.failures = 10 ** 6
        self.setup.ACCOUNT_SECONDS = 0
        self.setup.FINISH_ATTEMPTS = 2
        self.setup.begin(AGENT)
        self.link_phone()
        self.pairings()[0].exit(0)
        wait_until(lambda: self.setup.status()["state"] == "failed")
        self.assertEqual(self.setup.status()["note"], setup.NOTES["finish_failed"])
        FakeClient.failures = 0
        self.assertEqual(self.setup.begin(AGENT)["state"], "finishing")
        wait_until(lambda: not binding.setup_marker_path(self.root).exists())
        self.assert_finished()
        self.assertEqual(len(self.pairings()), 1)

    def test_a_linked_read_only_binding_gains_sending_on_start_without_relinking(self):
        self.hand_binding(1)
        self.link_phone()
        self.setup.start()
        self.assertFalse(self.receiver.managed)
        self.host.ready = False
        self.assertEqual(self.setup.status(), {"state": "connected", "note": setup.NOTES["read_only"], "qr": False,
                                               "send_ready": False})
        self.assertEqual(self.host.workers, 0)
        self.host.ready = True
        self.assertEqual(self.setup.begin(AGENT)["state"], "finishing")
        wait_until(lambda: not binding.setup_marker_path(self.root).exists())
        self.assert_finished()
        self.assertEqual(self.pairings(), [])

    def test_a_binding_set_up_by_hand_is_left_exactly_as_it_is(self):
        self.hand_binding(2)
        self.link_phone()
        before = binding.root_binding(self.root).read_bytes()
        self.host.ready = "always"
        self.assertEqual(self.setup.status()["state"], "connected")
        self.assertEqual(self.setup.begin(AGENT)["state"], "connected")
        self.assertEqual(binding.root_binding(self.root).read_bytes(), before)
        self.assertFalse(binding.setup_marker_path(self.root).exists())
        self.assertEqual((self.host.workers, self.host.grants, self.host.restarts, self.pairings()), (0, [], [], []))

    def test_a_phone_linked_for_another_ordubashy_is_refused(self):
        self.hand_binding(1)
        self.link_phone()
        with self.assertRaises(DomainError) as caught:
            self.setup.begin(OTHER)
        self.assertEqual(caught.exception.code, "forbidden")


class _Unit:
    def __enter__(self):
        return NS(connection=None)

    def __exit__(self, *_):
        return False


class AccessTest(unittest.TestCase):
    def tool(self):
        app = NS(current_context=lambda ctx, uow: ctx, database=NS(read=lambda: _Unit()))
        return setup.SetupTool(app, NS())

    def caller(self, ctx, coordinator=AGENT):
        with mock.patch("qorqut.management.identity.configured_ordubashy", return_value=coordinator):
            return self.tool().caller(ctx, NS())

    def test_only_the_configured_global_ordubashy_in_person(self):
        from dataclasses import replace
        from qorqut_operations.contracts import InvocationContext, Principal
        agent = InvocationContext(Principal(AGENT, "agent"), source="mcp")
        self.assertEqual(self.caller(agent), AGENT)
        for refused in (InvocationContext(Principal(OTHER, "agent"), source="mcp"),
                        replace(agent, delegation_assignment_id=OTHER, delegation_binding_id=OTHER, delegation_generation=1),
                        InvocationContext(Principal(AGENT, "human"), source="dashboard")):
            with self.assertRaises(DomainError) as caught:
                self.caller(refused)
            self.assertEqual(caught.exception.code, "forbidden")
        with self.assertRaises(DomainError):
            self.caller(agent, coordinator=None)

    def test_policy_offers_the_tool_to_ordubashy_only(self):
        from qorqut.composition.catalog import builtin_registry
        from qorqut.composition.policy import resolve_operation_policy
        from qorqut.extensions import ExtensionSet
        from qorqut_operations.background import ProgramRegistry
        extension = create()
        registry = builtin_registry(ExtensionSet((extension,)))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        extension.install(NS(paths=NS(instance_root=Path(folder.name)), registry=registry, programs=ProgramRegistry(),
                             application=NS(database=None)))
        descriptor = registry.get(setup.TOOL)
        self.assertEqual((descriptor.effect_kind, descriptor.supports_signoff, set(descriptor.exposure)), ("records", False, {"mcp"}))
        for role, mode in (("ordubashy", "allow"), ("global-worker", "deny"), ("learning-applier", "deny")):
            self.assertEqual(resolve_operation_policy(setup.TOOL, {}, {}, registry, role_id=role).mode, mode, role)
        self.assertEqual(resolve_operation_policy(setup.TOOL, {}, {setup.TOOL: "deny"}, registry, role_id="ordubashy").mode, "deny")
        self.assertIn("Settings > Connections > WhatsApp", descriptor.description)


class InstalledTest(unittest.TestCase):
    """The tool and the host steps against a real store: the engine's own prepare, external and apply path, the program
    worker registration and the grant check."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from private_thread_runtime import build_runtime
        from qorqut_operations.background import BackgroundAuthority, ProgramRegistry
        from qorqut_whatsapp.extension import register_program
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = build_runtime(Path(directory.name))
        self.addCleanup(self.runtime.database.close)
        self.runtime.programs = ProgramRegistry()
        self.runtime.paths.instance_root = Path(directory.name)
        operations = WhatsAppSendOperations(self.runtime.application, self.runtime.paths, self.runtime.programs)
        register_program(self.runtime.programs, operations)
        BackgroundAuthority(self.runtime.database, self.runtime.programs)
        self.views = []
        fake = NS(status=lambda: {"state": "not_set_up", "note": "n", "qr": False, "send_ready": False},
                  begin=lambda agent_id: self.views.append(agent_id) or {"state": "linking", "note": "l", "qr": True, "send_ready": False},
                  cancel=lambda: {"state": "not_set_up", "note": "n", "qr": False, "send_ready": False})
        setup.SetupTool(self.runtime.application, fake).register(self.runtime.application.registry)
        self.host = setup.RuntimeHost(self.runtime)

    def call(self, ctx, action):
        from qorqut_operations.contracts import InvocationOptions, new_id, plain
        result = self.runtime.application.invoke(ctx, setup.TOOL, {"action": action}, InvocationOptions(idempotency_key=new_id()))
        return plain(result.data) if result.status == "ok" else plain(result.error)["code"]

    def test_the_coordinator_runs_the_tool_and_everyone_else_is_refused(self):
        coordinator = self.runtime.coordinator.principal_id
        self.assertEqual(self.call(self.runtime.coordinator, "status"), {"state": "not_set_up", "explanation": "n"})
        answer = self.call(self.runtime.coordinator, "start")
        self.assertEqual((answer["state"], answer["next"]), ("linking", setup.ASK_OWNER))
        self.assertEqual(self.views, [coordinator])
        for ctx in (self.runtime.worker, self.runtime.owner):
            self.assertEqual(self.call(ctx, "start"), "forbidden")
        self.assertEqual(self.views, [coordinator])
        self.assertEqual(self.host.coordinator(), coordinator)

    def test_the_worker_is_registered_once_and_sending_needs_the_grant(self):
        coordinator = self.runtime.coordinator.principal_id
        self.assertFalse(self.host.send_ready(coordinator))
        self.host.register_worker()
        self.host.register_worker()
        self.assertFalse(self.host.send_ready(coordinator))
        with self.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.tool_policy',json(?)) "
                                   "WHERE agent_id=?", (json.dumps({"overrides": {"whatsapp_send": "signoff"}}), coordinator))
        self.assertTrue(self.host.send_ready(coordinator))
        self.assertTrue(self.host.grant(coordinator))      # already granted: nothing is changed
        with self.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.tool_policy',json(?)) "
                                   "WHERE agent_id=?", (json.dumps({"overrides": {"whatsapp_send": "deny"}}), coordinator))
        self.assertFalse(self.host.grant(coordinator))     # the owner's deny is kept
        self.assertFalse(self.host.send_ready(coordinator))


class Request:
    def __init__(self, body=None, headers=None):
        self.body = json.dumps(body).encode() if body is not None else b""
        self.headers = {"content-type": "application/json", "content-length": str(len(self.body)), **(headers or {})}

    async def stream(self):
        yield self.body


class RoutesTest(Base):
    def setUp(self):
        super().setUp()
        self.extension = WhatsApp()
        self.extension.setup = self.setup
        self.owner = "owner-1"
        self.handlers = {(route.method, route.path[-1]): route.handler for route in routes.owner_routes(self.extension)}

    def api(self, principal_kind="human", principal_id="owner-1"):
        installation = {"owner_principal_id": self.owner}
        unit = NS(repository=lambda name: NS(get=lambda key: installation))

        class Read:
            def __enter__(self):
                return unit

            def __exit__(self, *_):
                return False
        return NS(context=lambda actor: (NS(principal_kind=principal_kind, principal_id=principal_id), None),
                  runtime=NS(database=NS(read=Read)), statuses={})

    def call(self, method, name, request=None, api=None):
        handler = self.handlers[(method, name)]
        try:
            return asyncio.run(handler(api or self.api(), request or Request(), object(), "request-1"))
        except DomainError as problem:
            return problem

    def test_the_three_owner_routes_are_declared(self):
        self.assertEqual(set(self.handlers), {("GET", "setup"), ("POST", "setup"), ("GET", "qr")})
        for route in routes.owner_routes(self.extension):
            self.assertEqual(route.path[:3], ("root", "extensions", "whatsapp"))

    def test_the_owner_reads_the_state_links_and_cancels(self):
        answer = self.call("GET", "setup")
        self.assertEqual(answer.headers["cache-control"], "private, no-store")
        self.assertEqual(json.loads(answer.body)["data"], {"state": "not_set_up", "note": setup.NOTES["not_set_up"],
                                                          "qr": False, "send_ready": False})
        answer = self.call("POST", "setup", Request({"action": "link"}))
        self.assertEqual(json.loads(answer.body)["data"]["state"], "linking")
        self.assertEqual(self.binding_value()["agent_id"], AGENT)
        self.assertEqual(self.call("GET", "qr").code, "not_found")
        (binding.data_path(self.root) / "pair.png").write_bytes(b"\x89PNG-made-up")
        self.assertEqual(json.loads(self.call("GET", "setup").body)["data"]["qr"], True)
        image = self.call("GET", "qr")
        self.assertEqual((image.body, image.media_type, image.headers["cache-control"]), (b"\x89PNG-made-up", "image/png", "no-store"))
        answer = self.call("POST", "setup", Request({"action": "cancel"}))
        self.assertEqual(json.loads(answer.body)["data"]["state"], "not_set_up")
        self.assertEqual(self.call("GET", "qr").code, "not_found")

    def test_anyone_but_the_owners_dashboard_is_refused(self):
        self.assertEqual(self.call("GET", "setup", api=self.api("agent", AGENT)).code, "forbidden")
        self.assertEqual(self.call("GET", "qr", api=self.api("human", "someone-else")).code, "forbidden")
        self.assertEqual(self.call("GET", "setup", Request(headers={"authorization": "Bearer x"})).code, "forbidden")
        self.assertEqual(self.call("POST", "setup", Request({"action": "send"})).code, "validation_error")
        self.assertEqual(self.processes, [])

    def test_linking_needs_a_ready_ordubashy(self):
        self.host.coordinator = lambda: None
        self.assertEqual(self.call("POST", "setup", Request({"action": "link"})).code, "operation_unavailable")
        self.assertEqual(self.processes, [])


if __name__ == "__main__":
    unittest.main()
