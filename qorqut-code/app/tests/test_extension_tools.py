"""Ordubashy's extensions_install and extensions_remove on a real store with the real Signoff path: held for the owner,
approved, applied as one job, staged by that job, applied at the next start and reported to the asking agent."""
import json
from pathlib import Path
import sys
import shutil
from types import SimpleNamespace as NS
from unittest import mock
import unittest
from unittest import mock

from qorqut_operations.background import BackgroundAuthority, ProgramRegistry
from qorqut_operations.contracts import DomainError, InvocationOptions, PolicyDecision, new_id, plain
from qorqut_operations.storage.repositories import decode_row

from qorqut import connectors, extension_connections as connections, extension_index, extension_registry as registry
from qorqut.bootstrap import extension_change as change
from qorqut.extension_private import PrivateFolder
from qorqut.composition.policy import EXTENSION_DOWNLOADS, resolve_operation_policy
from qorqut.domains import extension_tools as tools

sys.path.insert(0, str(Path(__file__).resolve().parent))
from private_thread_runtime import Policy, build_runtime  # noqa: E402
from test_extension_change import ChangeCase  # noqa: E402


class HeldPolicy(Policy):
    """The fixture's policy, with the real rule for the two tools and the proposer's context for the Signoff path."""

    def __init__(self, runtime):
        self.runtime = runtime

    def resolve(self, ctx, descriptor, targets, uow):
        if descriptor.name in EXTENSION_DOWNLOADS:
            role = "ordubashy" if ctx.principal_id == self.runtime.coordinator.principal_id else "global-worker"
            mode = resolve_operation_policy(descriptor.name, {}, {}, self.runtime.application.registry, role_id=role,
                                            scope="global").mode
            return PolicyDecision(mode, "test", {"global_capability": descriptor.global_capability})
        return super().resolve(ctx, descriptor, targets, uow)

    def context_for(self, principal_id, **kwargs):
        if principal_id == self.runtime.coordinator.principal_id:
            return self.runtime.coordinator
        return super().context_for(principal_id, **kwargs)


class ToolsCase(ChangeCase):
    def setUp(self):
        super().setUp()
        self.rt = build_runtime(self.root / "store")
        self.addCleanup(self.rt.database.close)
        self.rt.application.policy = HeldPolicy(self.rt)
        self.programs = ProgramRegistry()
        BackgroundAuthority(self.rt.database, self.programs)
        self.found = self.registry()
        self.tools = tools.ExtensionTools(NS(application=self.rt.application, paths=self.paths, programs=self.programs,
                                             signoffs=self.rt.signoffs), self.found, index=lambda: self.index,
                                          app_version="1.7.0", platform="linux", machine="x86_64",
                                          restart=self.restart).register()
        with self.rt.database.transaction() as uow:
            self.tools.register_worker(uow)

    def registry(self, app_version="1.7.0", shipped=None):
        """The extensions found at a start, recorded in the store the tools read."""
        found = self.discover(app_version=app_version, shipped=shipped)
        listed = registry.ExtensionRegistry(found, offered=extension_index.offered(
            self.index, found, app_version=app_version, platform="linux", machine="x86_64"))
        listed.sync(self.rt.database, self.layer)
        if hasattr(self, "tools"):
            self.tools.extensions = listed
        return listed

    def ask(self, operation, ctx=None, extension="network"):
        return self.rt.application.invoke(ctx or self.rt.coordinator, operation, {"extension": extension},
                                          InvocationOptions(idempotency_key=new_id()))

    def held(self, operation, **arguments):
        result = self.ask(operation, **arguments)
        self.assertEqual(result.status, "signoff_required", plain(result.error) if result.error else None)
        return self.signoff(result.signoff["id"])

    def signoff(self, identity):
        with self.rt.database.read() as uow:
            return decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE id=?", (identity,)).fetchone())

    def binding(self, row):
        return dict(proposal_revision=row["proposal_revision"], payload_hash=row["payload_hash"],
                    expected_status_revision=row["status_revision"], idempotency_key=new_id())

    def approve(self, row):
        result = self.rt.signoffs.review(self.rt.owner, row["id"], decision="approve", **self.binding(row))
        self.assertEqual(result.status, "accepted", plain(result.error) if result.error else None)
        self.rt.signoffs.run_one(row["id"])
        self.assertEqual(self.signoff(row["id"])["state"], "applied", self.signoff(row["id"])["error_json"])
        with self.rt.database.read() as uow:
            return decode_row(uow.connection.execute("SELECT * FROM jobs WHERE kind=? ORDER BY rowid DESC LIMIT 1",
                                                     (tools.PROGRAM,)).fetchone())

    def run_job(self, job):
        control = NS(job=job, service=NS(database=self.rt.database), check=lambda **_: None)
        return tools.ChangeProgram(self.tools).run(control, job["arguments_json"])

    def notes(self, signoff_id):
        with self.rt.database.read() as uow:
            return [row[0] for row in uow.connection.execute(
                "SELECT body FROM signoff_notes WHERE signoff_id=? ORDER BY sequence", (signoff_id,))]

    def delivered(self, agent_id, signoff_id):
        with self.rt.database.read() as uow:
            return [row[0] for row in uow.connection.execute(
                "SELECT e.event_type FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                "WHERE d.agent_id=? AND e.source_type='signoffs' AND e.source_id=? ORDER BY e.id", (agent_id, signoff_id))]

    def next_start(self, shipped=None):
        """Stopped, then started: the change is applied before the selection, found, recorded and reported."""
        change.apply_pending(self.paths)
        self.registry(shipped=shipped)
        return change.finish(self.paths, report=self.tools.report)


class PolicyTest(ToolsCase):
    def test_ordubashy_is_held_for_the_owner_and_every_other_role_is_denied(self):
        rules = self.rt.application.registry
        for name in EXTENSION_DOWNLOADS:
            self.assertEqual(resolve_operation_policy(name, {}, {}, rules, role_id="ordubashy", scope="global").mode, "signoff")
            self.assertEqual(resolve_operation_policy(name, {}, {name: "allow"}, rules, role_id="ordubashy",
                                                      scope="global").mode, "signoff")
            self.assertEqual(resolve_operation_policy(name, {}, {name: "deny"}, rules, role_id="ordubashy",
                                                      scope="global").mode, "deny")
            for role in ("global-worker", "learning-applier", "project-worker"):
                self.assertEqual(resolve_operation_policy(name, {name: "allow"}, {}, rules, role_id=role,
                                                          scope="global").mode, "deny", role)


class ExtensionToolsTableTest(ToolsCase):
    """Who reaches each extension tool, with step 3's rules and this step's together, on the real rules of every tool."""

    TABLE = {  # tool: (Ordubashy, the Learning Applier, a worker), in Global
        "extensions_list": ("allow", "allow", "deny"),
        "extensions_setup": ("allow", "deny", "deny"),
        "extensions_switch": ("allow", "deny", "deny"),
        "extensions_install": ("signoff", "deny", "deny"),
        "extensions_remove": ("signoff", "deny", "deny"),
    }

    def test_the_whole_table(self):
        from qorqut.extension_operations import register as register_step_three
        rules = self.rt.application.registry
        register_step_three(rules, self.found)
        for name, modes in self.TABLE.items():
            for role, mode in zip(("ordubashy", "learning-applier", "global-worker"), modes):
                for overrides in ({}, {name: "allow"}):
                    with self.subTest(name=name, role=role, overrides=overrides):
                        self.assertEqual(resolve_operation_policy(name, {}, overrides, rules, role_id=role,
                                                                  scope="global").mode, mode)
        # The tool listing judges in the project scope: there too Ordubashy's two are a Signoff, so they are listed for
        # him (the handler admits him only in Global, ``test_only_ordubashy_in_person...``).
        for name in EXTENSION_DOWNLOADS:
            self.assertEqual(resolve_operation_policy(name, {}, {}, rules, role_id="ordubashy").mode, "signoff")


class InstallTest(ToolsCase):
    def test_ordubashy_asks_and_another_agent_is_denied(self):
        row = self.held(tools.INSTALL_TOOL)
        self.assertEqual((row["category"], row["state"], row["proposer_agent_id"]),
                         ("internal_change", "pending", self.rt.coordinator.principal_id))
        denied = self.ask(tools.INSTALL_TOOL, ctx=self.rt.worker)
        self.assertEqual((denied.status, plain(denied.error)["code"]), ("error", "policy_denied"))

    def test_the_card_is_short(self):
        row = self.held(tools.INSTALL_TOOL)
        preview = row["preview_json"]
        entry = self.index.entry("network")
        self.assertEqual(preview["title"], "Install the extension Network 1.0.0")
        self.assertEqual(tools.ExtensionTools.fields(preview), {
            "extension": "Network", "version": "1.0.0", "summary": entry.summary, "after_approval": "Qorqbot restarts once."})
        self.assertNotIn("amend_and_approve", preview)
        self.assertEqual([item["resource"]["resource_type"] for item in preview["changes"]], ["extension"])

    def test_approving_stages_exactly_what_was_bound_even_if_the_index_changed(self):
        row = self.held(tools.INSTALL_TOOL)
        bound = self.index.entry("network").bundles[0]
        job = self.approve(row)
        self.assertEqual(job["arguments_json"], {"scope": "global", "action": "install", "extension": "network",
                                                 "version": "1.0.0", "url": bound.url, "sha256": bound.sha256,
                                                 "size": bound.size, "platform": bound.platform, "delete_data": False})
        self.index = self.update_source()          # the index now offers 1.0.1
        self.run_job(job)
        pending = change.pending(self.paths)
        self.assertEqual((pending["action"], pending["version"], pending["sha256"], pending["requested_by"],
                          pending["signoff_id"]), ("install", "1.0.0", bound.sha256, self.rt.coordinator.principal_id, row["id"]))
        self.assertEqual(self.restarts, ["install extension network 1.0.0"])
        # A second run of the same job, as after the restart it asked for, stages nothing more.
        self.run_job(job)
        self.assertEqual(len(self.restarts), 1)

    def test_the_result_reaches_the_asking_agent_after_finish(self):
        row = self.held(tools.INSTALL_TOOL)
        self.run_job(self.approve(row))
        before = self.delivered(self.rt.coordinator.principal_id, row["id"])
        self.assertIsNotNone(self.next_start())
        self.assertTrue((self.installed() / "extension.yaml").is_file())
        self.assertEqual(self.notes(row["id"])[-1], "Installed Network 1.0.0. Qorqbot restarted.")
        self.assertEqual(self.delivered(self.rt.coordinator.principal_id, row["id"])[len(before):], ["note_added"])
        self.assertIsNone(change.pending(self.paths))

    def test_an_update_says_the_texts_are_replaced(self):
        self.place()
        self.registry()
        refused = self.ask(tools.INSTALL_TOOL)
        self.assertEqual(plain(refused.error)["message"], "Network 1.0.0 is installed, and the index has no newer version.")
        self.index = self.update_source()
        row = self.held(tools.INSTALL_TOOL)
        self.assertEqual(row["preview_json"]["title"], "Update the extension Network to 1.0.1")
        self.assertEqual(tools.ExtensionTools.fields(row["preview_json"])["after_approval"],
                         "Qorqbot restarts once. Changes made to the extension's texts on this installation are replaced.")
        self.run_job(self.approve(row))
        self.next_start()
        self.assertEqual(self.notes(row["id"])[-1], "Updated Network to 1.0.1. Qorqbot restarted.")

    def test_a_job_run_again_after_its_change_was_applied_or_failed_stages_nothing(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                row = self.held(tools.INSTALL_TOOL)
                job = self.approve(row)
                self.run_job(job)
                if broken:
                    (Path(change.pending(self.paths)["staged"]) / "README.md").unlink()
                self.next_start()
                reported, restarts = self.notes(row["id"]), list(self.restarts)
                self.assertTrue(reported[-1].startswith("Network was not installed." if broken else "Installed Network"))
                # The process was killed before the job was recorded as finished: it runs once more.
                self.run_job(job)
                self.assertEqual((self.notes(row["id"]), self.restarts, change.pending(self.paths)),
                                 (reported, restarts, None))
                shutil.rmtree(self.installed(), ignore_errors=True)
                self.registry()

    def test_a_job_without_an_approved_signoff_behind_it_is_refused(self):
        row = self.held(tools.INSTALL_TOOL)
        job = self.approve(row)
        for forged in ({**job, "definition_ref_json": {**job["definition_ref_json"], tools.MARKER: None}},
                       {**job, "id": new_id()},
                       {**job, "arguments_json": {**job["arguments_json"], "version": "1.0.1"}}):
            with self.assertRaises(DomainError) as refused:
                tools.ChangeProgram(self.tools).run(NS(job=forged, service=NS(database=self.rt.database),
                                                       check=lambda **_: None), forged["arguments_json"])
            self.assertEqual(refused.exception.code, "forbidden")
        self.assertEqual((change.pending(self.paths), self.restarts), (None, []))

    def test_only_ordubashy_in_person_even_where_the_policy_would_let_another_through(self):
        self.rt.application.policy.resolve = lambda ctx, descriptor, targets, uow: PolicyDecision(
            "signoff", "test", {"global_capability": descriptor.global_capability})
        for operation in (tools.INSTALL_TOOL, tools.REMOVE_TOOL):
            refused = self.ask(operation, ctx=self.rt.worker)
            self.assertEqual((plain(refused.error)["code"], plain(refused.error)["message"]), ("forbidden", tools.ONLY_ORDUBASHY))

    def test_the_worker_is_written_once_and_an_occupied_id_is_refused(self):
        with self.rt.database.read() as uow:
            worker = self.programs.authority.worker_id(tools.PROGRAM, uow)
        def rows():
            with self.rt.database.read() as uow:
                return [tuple(row) for row in uow.connection.execute(
                    "SELECT kind, display_name FROM principals WHERE id=?", (worker,))]
        with self.rt.database.transaction() as uow:
            self.tools.register_worker(uow)       # a later start writes nothing
        self.assertEqual(rows(), [("system", "extensions.change worker")])
        with self.rt.database.transaction() as uow:
            uow.connection.execute("UPDATE principals SET kind='agent' WHERE id=?", (worker,))
        with self.assertRaises(DomainError) as occupied, self.rt.database.transaction() as uow:
            self.tools.register_worker(uow)
        self.assertEqual(occupied.exception.code, "invalid_principal")

    def test_approving_after_the_owner_installed_it_himself_is_refused_and_queues_nothing(self):
        row = self.held(tools.INSTALL_TOOL)
        card = OwnerCardTest.click.__get__(self)
        self.runtime = NS(database=self.rt.database, application=self.rt.application, extension_setups=None,
                          extension_tools=self.tools)
        card(action="install", version="1.0.0")
        # While his own change waits, and after it was applied, Ordubashy's card is refused with the plain sentence.
        for sentence in (change.WAITING, "Network 1.0.0 is installed, and the index has no newer version."):
            result = self.rt.signoffs.review(self.rt.owner, row["id"], decision="approve", **self.binding(row))
            self.assertEqual((result.status, plain(result.error)["message"]), ("error", sentence))
            row = self.signoff(row["id"])
            self.assertEqual(row["state"], "pending")
            self.next_start()
        with self.rt.database.read() as uow:
            self.assertIsNone(uow.connection.execute("SELECT 1 FROM jobs WHERE kind=?", (tools.PROGRAM,)).fetchone())
        self.assertEqual(self.restarts, ["install extension network 1.0.0"])

    def test_installed_but_not_usable_says_why(self):
        row = self.held(tools.INSTALL_TOOL)
        self.run_job(self.approve(row))
        # The start finds that a skill Qorqbot already has takes the name of the extension's skill.
        self.next_start(shipped=lambda: frozenset({"qorqut-plugin-network"}))
        entry = self.tools.extensions.get("network")
        self.assertEqual(entry.status, "needs_attention")
        self.assertEqual(self.notes(row["id"])[-1], f"Installed Network 1.0.0. Qorqbot restarted. It cannot be used yet: {entry.note}")

    def test_a_failure_before_the_restart_is_reported_and_changes_nothing(self):
        row = self.held(tools.INSTALL_TOOL)
        job = self.approve(row)
        bundle = Path(self.index.entry("network").bundles[0].url.removeprefix("file://"))
        bundle.write_bytes(bundle.read_bytes()[:-1] + b"x")
        with self.assertRaises(DomainError) as failed:
            self.run_job(job)
        sentence = "The downloaded bundle does not have the checksum the index gives."
        self.assertEqual(failed.exception.message, sentence)
        self.assertEqual(self.notes(row["id"])[-1], f"Network was not installed. {sentence}")
        self.assertIn("note_added", self.delivered(self.rt.coordinator.principal_id, row["id"]))
        self.assertEqual((change.pending(self.paths), self.restarts, self.installed().exists()), (None, [], False))
        # The owner's card says it too, marked failed.
        card = connections.current(NS(database=self.rt.database, application=self.rt.application, extension_setups=None,
                                      extension_tools=self.tools, extension_registry=self.tools.extensions), "network")
        self.assertEqual((card["result"], card["failed"]), (f"Network was not installed. {sentence}", True))

    def test_what_cannot_be_asked_for(self):
        self.assertEqual(plain(self.ask(tools.INSTALL_TOOL, extension="crm_extra").error)["code"], "not_found")
        self.index = extension_index.parse({**json.loads((self.release / "out-1.0.0" / extension_index.INDEX_FILE).read_text()),
                                            "extensions": [{**json.loads((self.release / "out-1.0.0" / extension_index.INDEX_FILE)
                                                                          .read_text())["extensions"][0], "platforms": ["mac"]}]})
        self.assertEqual(plain(self.ask(tools.INSTALL_TOOL).error)["message"], "It runs only on mac, not on linux.")

    def test_another_change_waiting_is_refused(self):
        self.held(tools.INSTALL_TOOL)
        self.assertTrue(self.stage().staged)
        self.assertEqual(plain(self.ask(tools.INSTALL_TOOL).error)["message"], change.WAITING)


class RemoveTest(ToolsCase):
    def setUp(self):
        super().setUp()
        self.place()
        self.registry()
        self.private = PrivateFolder(self.paths.instance_root, "network").path
        self.private.mkdir(parents=True)
        (self.private / "token").write_text("kept or deleted")
        self.neighbour = PrivateFolder(self.paths.instance_root, "other").path
        self.neighbour.mkdir()
        (self.neighbour / "token").write_text("never touched")

    def test_the_card_offers_remove_and_remove_and_delete_its_data(self):
        row = self.held(tools.REMOVE_TOOL)
        preview = row["preview_json"]
        self.assertEqual((preview["title"], preview["approve_label"]), ("Remove the extension Network", "Remove"))
        self.assertEqual(preview["amend_and_approve"], {"label": "Remove and delete its data", "changes": {"delete_data": True}})
        self.assertEqual(tools.ExtensionTools.fields(preview)["after_approval"],
                         "Qorqbot restarts once. Projects, tables and records it created stay.")
        detail = plain(self.rt.signoffs.get(self.rt.owner, row["id"]).data)["signoff"]
        self.assertEqual(detail["editable_fields"], ["delete_data"])
        self.assertEqual(plain(self.ask(tools.REMOVE_TOOL, ctx=self.rt.worker).error)["code"], "policy_denied")

    def test_remove_keeps_the_private_folder(self):
        row = self.held(tools.REMOVE_TOOL)
        self.run_job(self.approve(row))
        self.assertFalse(change.pending(self.paths)["delete_data"])
        self.next_start()
        self.assertFalse(self.installed().exists())
        self.assertEqual((self.private / "token").read_text(), "kept or deleted")
        self.assertEqual(self.notes(row["id"])[-1], "Removed Network. Qorqbot restarted.")

    def test_remove_and_delete_its_data_deletes_only_its_private_folder(self):
        row = self.held(tools.REMOVE_TOOL)
        edited = self.rt.signoffs.amend(self.rt.owner, row["id"], changes={"delete_data": True}, **self.binding(row))
        self.assertEqual(edited.status, "ok", plain(edited.error) if edited.error else None)
        new = self.signoff(plain(edited.data)["signoff"]["id"])
        self.assertEqual(self.notes(new["id"]), [tools.DELETE_NOTE])
        self.assertNotIn("amend_and_approve", new["preview_json"])
        self.assertEqual(new["preview_json"]["approve_label"], "Remove and delete its data")
        # The same sentence as the Connections card: the button says what happens to the private data.
        self.assertEqual(tools.ExtensionTools.fields(new["preview_json"])["after_approval"],
                         "Qorqbot restarts once. Projects, tables and records it created stay.")
        job = self.approve(new)
        self.assertTrue(job["arguments_json"]["delete_data"])
        self.run_job(job)
        self.next_start()
        self.assertFalse(self.private.exists())
        self.assertEqual((self.neighbour / "token").read_text(), "never touched")
        self.assertEqual(self.notes(new["id"])[-1], "Removed Network. Qorqbot restarted. Its private data was deleted.")
        # Ordubashy hears of the replacement and of its result, and nothing of his own proposal ending: the owner's edit
        # replaced it, which is no outcome of its own.
        ordubashy = self.rt.coordinator.principal_id
        self.assertEqual(self.delivered(ordubashy, row["id"]), [])
        self.assertEqual(self.delivered(ordubashy, new["id"])[-2:], ["action_outcome", "note_added"])
        self.assertEqual(self.signoff(row["id"])["state"], "conflicted")

    def test_the_owner_can_choose_nothing_else(self):
        row = self.held(tools.REMOVE_TOOL)
        for changes in ({"delete_data": False}, {"extension": "other"}):
            edited = self.rt.signoffs.amend(self.rt.owner, row["id"], changes=changes, **self.binding(row))
            self.assertEqual(plain(edited.error)["code"], "validation_error", changes)

    def test_a_linked_private_folder_is_left_as_it_is(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("outside")
        for item in self.private.iterdir():
            item.unlink()
        self.private.rmdir()
        self.private.symlink_to(outside)
        self.assertTrue(change.stage_removal(self.paths, "network", delete_data=True, restart=self.restart).staged)
        change.apply_pending(self.paths)
        applied = change.pending(self.paths)
        self.assertEqual(applied["result"], {"state": "done", "note": change.NOT_DELETED})
        self.assertEqual(tools.result_sentence("Network", applied),
                         "Removed Network. Qorqbot restarted. Its private data was not deleted, because its private folder is not a plain folder.")
        self.assertEqual((outside / "keep").read_text(), "outside")

    def test_a_deletion_that_fails_partway_does_not_stop_the_start_and_is_reported(self):
        (self.private / "file").write_text("cannot go")
        real = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            # The deletion of the private folder fails whoever runs the test; every other one goes ahead.
            if Path(path) == self.private:
                raise PermissionError("locked")
            return real(path, *args, **kwargs)
        row = self.held(tools.REMOVE_TOOL)
        edited = self.rt.signoffs.amend(self.rt.owner, row["id"], changes={"delete_data": True}, **self.binding(row))
        new = self.signoff(plain(edited.data)["signoff"]["id"])
        self.run_job(self.approve(new))
        with mock.patch.object(shutil, "rmtree", rmtree), \
                self.assertLogs("qorqut.bootstrap.extension_change", "WARNING") as logged:
            self.next_start()
        self.assertIn("extension_data_not_deleted extension=network error=PermissionError", " ".join(logged.output))
        self.assertNotIn("locked", " ".join(logged.output))
        self.assertFalse(self.installed().exists())
        self.assertTrue((self.private / "file").exists())
        self.assertEqual(self.notes(new["id"])[-1], "Removed Network. Qorqbot restarted. Its private data could not be deleted completely.")

    def test_a_folder_whose_manifest_cannot_be_read_can_be_removed_and_is_named_by_its_id(self):
        (self.installed() / "extension.yaml").write_text("id: [not a manifest\n")
        self.registry()
        row = self.held(tools.REMOVE_TOOL)
        self.assertEqual(row["preview_json"]["title"], "Remove the extension network")
        runtime = NS(database=self.rt.database, application=self.rt.application, extension_setups=None,
                     extension_tools=self.tools, extension_registry=self.tools.extensions)
        self.assertEqual(connections.current(runtime, "network")["offers"], [{"action": "remove"}])
        connections.act(runtime, self.rt.owner, "network", {"action": "remove", "delete_data": False})
        self.next_start()
        self.assertFalse(self.installed().exists())
        self.assertEqual(self.tools.results["network"], "Removed Network. Qorqbot restarted.")

    def test_a_built_in_or_missing_extension_cannot_be_removed(self):
        self.assertEqual(plain(self.ask(tools.REMOVE_TOOL, extension="crm_extra").error)["code"], "not_found")


class OwnerCardTest(ToolsCase):
    """The owner's own Install, Update and Remove on the Connections card: his click is the approval, so no Signoff is
    made; the same checks, staging, restart and result as the tools, and the result on the card after the restart."""

    def setUp(self):
        super().setUp()
        self.runtime = NS(database=self.rt.database, application=self.rt.application, extension_setups=None,
                          extension_tools=self.tools)

    def card(self):
        self.runtime.extension_registry = self.tools.extensions
        return connections.current(self.runtime, "network")

    def click(self, **body):
        self.runtime.extension_registry = self.tools.extensions
        return connections.act(self.runtime, self.rt.owner, "network", connections.parse_action(body))

    def signoffs(self):
        with self.rt.database.read() as uow:
            return uow.connection.execute("SELECT COUNT(*) FROM signoffs").fetchone()[0]

    def shown(self, entry):
        return {key: entry[key] for key in ("name", "status", "offers", "result", "restarting")}

    def test_install_update_and_remove_from_the_card(self):
        self.assertEqual(self.shown(self.card()), {"name": "Network", "status": "not_installed", "result": None,
                                                   "offers": [{"action": "install", "version": "1.0.0"}], "restarting": False})
        waiting = self.click(action="install", version="1.0.0")
        self.assertEqual(self.shown(waiting), {"name": "Network", "status": "not_installed", "offers": [],
                                               "result": tools.RESTARTING, "restarting": True})
        self.assertEqual(self.restarts, ["install extension network 1.0.0"])
        self.next_start()
        after = self.card()
        self.assertEqual((after["status"], after["result"], after["offers"], after["restarting"]),
                         ("ready", "Installed Network 1.0.0. Qorqbot restarted.", [{"action": "remove"}], False))
        self.index = self.update_source()
        self.registry()
        self.assertEqual(self.card()["offers"], [{"action": "update", "version": "1.0.1"}, {"action": "remove"}])
        self.assertEqual(self.refused("index_changed", action="update", version="1.0.2"), "The index has changed since the card was shown. Look again.")
        self.click(action="update", version="1.0.1")
        self.next_start()
        self.assertEqual(self.card()["result"], "Updated Network to 1.0.1. Qorqbot restarted.")
        private = PrivateFolder(self.paths.instance_root, "network").path
        private.mkdir(parents=True)
        (private / "token").write_text("deleted")
        self.click(action="remove", delete_data=True)
        self.next_start()
        gone = self.card()
        self.assertEqual((gone["status"], gone["result"], gone["offers"]),
                         ("not_installed", "Removed Network. Qorqbot restarted. Its private data was deleted.",
                          [{"action": "install", "version": "1.0.1"}]))
        self.assertFalse(private.exists())
        self.assertEqual(self.signoffs(), 0)

    def test_a_change_that_fails_at_the_start_that_applies_it_is_marked_failed_on_the_card(self):
        waiting = self.click(action="install", version="1.0.0")
        self.assertEqual((waiting["result"], waiting["failed"]), (tools.RESTARTING, False))
        # The staged folder is gone when the start comes: the change fails there, after the restart.
        shutil.rmtree(change.staging(self.paths))
        self.next_start()
        after = self.card()
        self.assertEqual((after["result"], after["failed"]),
                         ("Network was not installed. The staged folder is gone, so nothing was changed.", True))
        # Installed, but what the start found makes it unusable: a failure to the owner as well.
        self.click(action="install", version="1.0.0")
        self.next_start(shipped=lambda: frozenset({"qorqut-plugin-network"}))
        after = self.card()
        self.assertTrue(after["result"].startswith("Installed Network 1.0.0. Qorqbot restarted. It cannot be used yet:"))
        self.assertTrue(after["failed"])
        self.click(action="remove", delete_data=False)
        self.next_start()
        self.assertEqual((self.card()["result"], self.card()["failed"]), ("Removed Network. Qorqbot restarted.", False))

    def test_staging_judges_with_the_extensions_whose_code_is_on_the_path_now(self):
        self.tools.extensions.code = {"other": NS(folder=self.root / "other")}
        with mock.patch.object(change, "stage", wraps=change.stage) as staged:
            self.click(action="install", version="1.0.0")
        self.assertEqual(staged.call_args.kwargs["counted"], {"other"})

    def test_remove_keeps_the_data_unless_chosen(self):
        self.place()
        self.registry()
        private = PrivateFolder(self.paths.instance_root, "network").path
        private.mkdir(parents=True)
        (private / "token").write_text("kept")
        self.click(action="remove", delete_data=False)
        self.next_start()
        self.assertEqual(((private / "token").read_text(), self.card()["result"]), ("kept", "Removed Network. Qorqbot restarted."))

    def refused(self, reason, **body):
        """The answer to a click whose change was refused or failed before the restart: an ordinary answer, the entry with
        the sentence as its result, marked failed. It leaves one log line with the kind of reason."""
        with self.assertLogs("qorqut.bootstrap.extension_change", "WARNING") as written:
            entry = self.click(**body)
        self.assertTrue(entry.get("failed"), entry)
        self.assertEqual(written.output, [
            f"WARNING:qorqut.bootstrap.extension_change:extension_change_not_done action={body['action']} "
            f"extension=network requested_by={self.rt.owner.principal_id} reason={reason}"])
        return entry["result"]

    def test_a_failure_before_the_restart_is_said_and_changes_nothing(self):
        bundle = Path(self.index.entry("network").bundles[0].url.removeprefix("file://"))
        bundle.write_bytes(bundle.read_bytes()[:-1] + b"x")
        self.assertEqual(self.refused("checksum", action="install", version="1.0.0"),
                         "Network was not installed. The downloaded bundle does not have the checksum the index gives.")
        self.assertEqual((change.pending(self.paths), self.restarts, self.card()["offers"]),
                         (None, [], [{"action": "install", "version": "1.0.0"}]))
        # The card read afterwards keeps the sentence, marked failed.
        self.assertEqual((self.card()["result"], self.card()["failed"]),
                         ("Network was not installed. The downloaded bundle does not have the checksum the index gives.",
                          True))

    def test_two_changes_at_once_stage_one_and_the_second_touches_nothing(self):
        import threading
        inside, release, answers = threading.Event(), threading.Event(), {}
        real = change._download

        def slow(url, target, size):
            real(url, target, size)
            inside.set()
            release.wait(10)
        with mock.patch.object(change, "_download", slow):
            first = threading.Thread(target=lambda: answers.setdefault("owner", self.click(action="install", version="1.0.0")))
            first.start()
            self.assertTrue(inside.wait(10))
            # The job of an approved card comes while the owner's change is half staged: it waits its turn, then finds
            # the change waiting and stages nothing.
            second = threading.Thread(target=lambda: answers.setdefault("job", change.stage(
                self.paths, NS(id="network", version="1.0.0"), self.index.entry("network").bundles[0], restart=self.restart)))
            second.start()
            second.join(0.5)
            self.assertTrue(second.is_alive())
            release.set()
            first.join(10), second.join(10)
        self.assertEqual(answers["job"], change.Outcome(False, change.WAITING))
        self.assertTrue(Path(change.pending(self.paths)["staged"]).is_dir())
        self.assertEqual(self.restarts, ["install extension network 1.0.0"])

    def test_closed_for_writes_stages_nothing(self):
        with self.rt.database.transaction() as uow:
            uow.repository("maintenance_state").insert({"id": "service", "state": "closed", "message": "Recovery"})
        self.assertEqual(self.refused("maintenance", action="install", version="1.0.0"), "Recovery")
        self.assertEqual((change.pending(self.paths), self.restarts), (None, []))

    def test_no_restart_asked_for_says_so_and_does_not_wait_for_one(self):
        self.tools.restart = lambda reason: False
        entry = self.click(action="install", version="1.0.0")
        self.assertEqual((entry["result"], entry["restarting"]),
                         ("Qorqbot could not be asked to restart, so the change is applied at its next start.", False))

    def test_what_the_card_can_send(self):
        for body in ({"action": "install"}, {"action": "install", "version": ""}, {"action": "remove"},
                     {"action": "remove", "delete_data": "yes"}, {"action": "update", "version": "1.0.1", "extra": 1}):
            with self.assertRaises(DomainError):
                connections.parse_action(body)
        self.assertEqual(connections.parse_action({"action": "remove", "delete_data": True}),
                         {"action": "remove", "delete_data": True})
        self.click(action="install", version="1.0.0")
        self.assertEqual(self.refused("waiting", action="install", version="1.0.0"), change.WAITING)
        # The card keeps showing the change that waits.
        self.assertEqual((self.card()["result"], self.card()["failed"]), (tools.RESTARTING, False))

    def test_an_address_that_does_not_answer_and_a_bundle_of_another_version_are_said(self):
        from dataclasses import replace as replaced
        real, entry = self.index, self.index.entry("network")

        def serving(bundle):
            # The index as the tools read it, with the one bundle address changed.
            return NS(entries=real.entries, entry=real.entry, entry_for=lambda ext_id, platform: (entry, bundle))
        self.index = serving(replaced(entry.bundles[0], url=(self.root / "nothing-here.tar.gz").as_uri()))
        self.assertTrue(self.refused("not_downloaded", action="install", version="1.0.0").startswith(
            "Network was not installed. The bundle could not be downloaded or unpacked:"))
        # The index names 1.0.0 but its address serves the bundle of 1.0.1.
        newer = self.update_source().entry("network").bundles[0]
        self.index = serving(newer)
        self.assertEqual(self.refused("other_version", action="install", version="1.0.0"),
                         "Network was not installed. The bundle holds network 1.0.1, not network 1.0.0.")
        self.assertEqual((change.pending(self.paths), self.restarts), (None, []))


class KeptSetupTest(ToolsCase):
    """Removal keeps the private folder so that it serves the extension when it comes back: an install that finds a kept
    key that still fits the fields is ``ready`` again. An install with nothing kept asks nothing more."""

    def setUp(self):
        super().setUp()
        from test_extension_change import builder, generator
        from qorqut.extension_setup import ExtensionSetups, KeySetup
        folder = self.release / "keyed-source" / "keyed"
        shutil.copytree(Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "keyed", folder)
        out = self.release / "keyed-out"
        out.mkdir()
        bundle = builder.build_folder(folder, "any", out / "keyed.tar.gz")["path"]
        (out / extension_index.INDEX_FILE).write_text(json.dumps(generator.build_index([folder], [bundle], "1.7.0", out.as_uri())))
        self.index = extension_index.load(out / extension_index.INDEX_FILE)
        self.registry()
        self.asked = []
        test = self

        def handler(ext_id):
            manifest = test.tools.extensions.get(ext_id).manifest
            return KeySetup(PrivateFolder(test.paths.instance_root, ext_id), manifest.setup_fields, check=lambda values: None)

        class Setups:
            @property
            def current(self):
                return ExtensionSetups(test.tools.extensions, handler)

            def kept(self, ext_id):
                test.asked.append(ext_id)
                return self.current.kept(ext_id)
        self.setups = Setups()
        self.tools.runtime = NS(extension_setups=self.setups)
        self.card = NS(database=self.rt.database, application=self.rt.application, extension_setups=None,
                       extension_tools=self.tools)

    def click(self, **body):
        self.card.extension_registry = self.tools.extensions
        return connections.act(self.card, self.rt.owner, "keyed", connections.parse_action(body))

    def test_installing_again_finds_the_kept_key(self):
        self.click(action="install", version="1.0.0")
        self.next_start()
        self.assertEqual((self.asked, self.tools.extensions.get("keyed").status), ([], "not_set_up"))
        self.setups.current.accept("keyed", {"token": "kept-token"})
        self.assertEqual(self.tools.extensions.get("keyed").status, "ready")
        self.click(action="remove", delete_data=False)
        self.next_start()
        self.assertTrue((PrivateFolder(self.paths.instance_root, "keyed").path / "setup.json").is_file())
        self.click(action="install", version="1.0.0")
        self.next_start()
        self.assertEqual((self.asked, self.tools.extensions.get("keyed").status), (["keyed"], "ready"))
        self.assertEqual(self.tools.results["keyed"], "Installed Keyed 1.0.0. Qorqbot restarted.")


class ToolSchemaTest(ToolsCase):
    def test_the_two_tools_offer_no_expected_revision(self):
        from qorqut.transport.mcp import tool_schema
        for name in (tools.INSTALL_TOOL, tools.REMOVE_TOOL):
            properties = tool_schema(self.rt.application.registry, name)["properties"]
            self.assertEqual((set(properties) - {"idempotency_key", "project_id", "review_project_id", "supersedes_signoff_id",
                                                 "expected_superseded_status_revision"}), {"extension"}, name)


class NothingNewTest(ToolsCase):
    """With nothing found and an index that offers nothing, none of the five tools that manage extensions is any agent's,
    so every agent's tools are as before; one index entry is enough for all five to follow their policy."""

    def resolver_check(self, found):
        from qorqut.agents.resolver import AgentConfigResolver, contract_tools_in_effect
        from qorqut.extension_operations import MANAGING
        listed = contract_tools_in_effect(found, None)
        resolver = NS(extensions=found)
        called = {name for name in MANAGING
                  if AgentConfigResolver.operation_available(resolver, None, None, NS(name=name), None)}
        return {name for name in MANAGING if listed(name)}, called

    def test_with_no_index_and_nothing_downloaded_no_agent_has_the_five(self):
        self.index = None
        found = self.registry()
        self.assertEqual((found.list(), found.in_use()), ([], False))
        self.assertEqual(self.resolver_check(found), (set(), set()))
        self.assertEqual(connectors._contract_items(NS(extension_registry=found, database=self.rt.database)), ([], set()))

    def test_one_index_entry_gives_all_five_their_policy(self):
        from qorqut.extension_operations import MANAGING
        found = self.registry()
        self.assertTrue(found.in_use())
        self.assertEqual(self.resolver_check(found), (set(MANAGING), set(MANAGING)))
        [entry] = found.list()
        self.assertEqual((entry.id, entry.status), ("network", "not_installed"))
        items, _ = connectors._contract_items(NS(extension_registry=found, database=self.rt.database, extension_tools=self.tools))
        self.assertEqual([(item["id"], item["status"]) for item in items], [("network", "not_installed")])


class AppUpdateTest(ToolsCase):
    """The owner's answer on app updates, for an extension installed through this step: the start after the update goes
    ahead, the extension is unsupported with the reason, reaches no agent, keeps its folder, private folder and layer copy,
    cannot be updated to a version that does not fit, and can still be removed."""

    def test_an_extension_that_no_longer_fits_is_unsupported_and_kept(self):
        row = self.held(tools.INSTALL_TOOL)
        self.run_job(self.approve(row))
        self.next_start()
        found = self.tools.extensions
        with self.rt.database.transaction() as uow:
            found.switch("network", self.rt.coordinator.principal_id, True, uow=uow)
        private = PrivateFolder(self.paths.instance_root, "network").path
        private.mkdir(parents=True)
        (private / "token").write_text("kept")
        layer_copy = self.layer.root / "plugins" / "network" / "hooks.yaml"
        self.assertTrue(layer_copy.is_file())
        # The app is updated to a version the extension does not admit; the next start goes ahead.
        change.apply_pending(self.paths)
        later = self.registry(app_version="1.6.1")
        [entry] = later.list()
        self.assertEqual((entry.status, entry.note), ("unsupported", "It needs Qorqbot >=1.7.0, and this is Qorqbot 1.6.1."))
        with self.rt.database.read() as uow:
            self.assertEqual(later.on_for(self.rt.coordinator.principal_id, connection=uow.connection), ())
        self.assertEqual((self.installed().is_dir(), (private / "token").read_text(), layer_copy.is_file()), (True, "kept", True))
        self.tools.app_version = "1.6.1"
        self.index = self.update_source()
        self.assertEqual(plain(self.ask(tools.INSTALL_TOOL).error)["message"],
                         "It needs Qorqbot >=1.7.0, and this is Qorqbot 1.6.1.")
        self.assertEqual(self.held(tools.REMOVE_TOOL)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
