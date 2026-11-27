"""Set-up and background work of contract extensions on the real registry, with the ``network`` (no set-up) and ``keyed``
(a key) examples in a temporary store and agentic layer. Qorqbot does not load the code part yet, so the tests build the
handler and the service from the example's code directly."""
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from types import SimpleNamespace
import unittest

from qorqut import connectors, extension_connections
from qorqut.extension_private import PrivateFolder
from qorqut.extension_setup import CHECK_FAILED, HANDLER_FAILED, NOT_LOADED, ExtensionSetups, GatedService
from qorqut.extensions import ServiceDeclaration, SetupStart, SetupStatus
from qorqut.service import stopping
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import BoundedWriter

from test_extension_registry import APPLIER, ORDUBASHY, StoreCase

KEYED = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "keyed"
sys.path.insert(0, str(KEYED / "code"))

from qorqut_keyed import extension as keyed  # noqa: E402

OWNER = SetupStart("owner")


class Spy:
    """A handler that records every call and passes it to the real one."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            return getattr(self.handler, name)(*args)
        return call


class KeyedCase(StoreCase):
    def place_keyed(self, mutate=None):
        folder = self.downloaded / "keyed"
        shutil.copytree(KEYED, folder)
        if mutate:
            self.manifest(folder, mutate)
        return folder

    def place_renamed(self, ext_id, addition=""):
        """A copy of the example under another id: its module, skill, tool and service carry ``ext_id``."""
        folder = self.downloaded / ext_id
        shutil.copytree(KEYED, folder)
        (folder / "code" / "qorqut_keyed").rename(folder / "code" / f"qorqut_{ext_id}")
        (folder / "plugin" / "skills" / "keyed").rename(folder / "plugin" / "skills" / ext_id)
        for path in folder.rglob("*"):
            if path.is_file():
                path.write_text(path.read_text().replace("keyed", ext_id))
        code = folder / "code" / f"qorqut_{ext_id}" / "extension.py"
        code.write_text(code.read_text() + addition)
        importlib.invalidate_caches()
        return folder

    def files_holding(self, value):
        """Every file holding ``value``, leaving out the extension folders (the example's code names its test token)."""
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")
                      if path.is_file() and not path.is_symlink() and not path.is_relative_to(self.downloaded)
                      and value.encode() in path.read_bytes())


class ExtensionSetupsTest(KeyedCase):
    def setUp(self):
        super().setUp()
        self.place_keyed()
        self.place()
        self.found = self.start()
        self.folder = PrivateFolder(self.paths.instance_root, "keyed")
        self.spy = Spy(keyed.KeySetup(self.folder, keyed.FIELDS, keyed.check))
        self.handlers = {"keyed": self.spy}
        self.setups = ExtensionSetups(self.found, self.handlers.get)

    def test_listing_asks_no_handler(self):
        self.assertEqual(self.setups.status("keyed").status, "not_set_up")
        self.read(lambda connection: extension_connections.items(self.found, connection))
        self.found.list()
        self.assertEqual(self.spy.calls, [])
        self.assertFalse(self.paths.instance_root.exists() and (self.paths.instance_root / "private").exists())

    def test_the_owner_sets_it_up_and_disconnects(self):
        heard = []
        self.found.subscribe(lambda changes: heard.extend(changes))
        entry = self.setups.start("keyed", SetupStart("ordubashy", ORDUBASHY))
        self.assertEqual((entry.status, entry.note), ("setting_up", None))
        self.assertEqual(self.found.on_for(ORDUBASHY), ())
        entry = self.setups.accept("keyed", {"token": "wrong-token-canary"})
        self.assertEqual((entry.status, entry.note), ("needs_attention", "Keyed did not accept this token."))
        self.assertEqual(self.files_holding("wrong-token-canary"), [])
        entry = self.setups.accept("keyed", {"token": keyed.TEST_TOKEN})
        self.assertEqual((entry.status, entry.note, entry.agents), ("ready", None, (ORDUBASHY,)))
        # Ready: in effect for Ordubashy by ``on_after_setup``, and the restart subscriber hears it.
        self.assertEqual(self.found.on_for(ORDUBASHY), ("keyed",))
        self.assertIn(("keyed", "status", (ORDUBASHY,)), [(c.extension, c.what, c.affected) for c in heard])
        # The token is in the private file only: not in the store, the agentic layer or any other file.
        self.assertEqual(self.files_holding(keyed.TEST_TOKEN), ["instance/private/extensions/keyed/setup.json"])
        self.assertEqual(stat.S_IMODE(os.stat(self.folder.path / "setup.json").st_mode), 0o600)
        self.assertEqual(self.setups.cancel("keyed").status, "ready")
        entry = self.setups.disconnect("keyed")
        self.assertEqual(entry.status, "not_set_up")
        self.assertEqual(self.found.on_for(ORDUBASHY), ())
        self.assertEqual(self.files_holding(keyed.TEST_TOKEN), [])

    def test_cancel_ends_setting_up(self):
        self.setups.start("keyed", OWNER)
        self.assertEqual(self.setups.cancel("keyed").status, "not_set_up")

    def test_a_refusal_leaves_the_status(self):
        self.setups.start("keyed", OWNER)
        with self.assertRaises(DomainError) as refusal:
            self.setups.accept("keyed", {"token": keyed.TEST_TOKEN, "other": "x"})
        self.assertEqual(refusal.exception.code, "validation_error")
        self.assertEqual(self.found.get("keyed").status, "setting_up")
        with self.assertRaises(DomainError):
            self.setups.start("keyed", "owner")

    def test_a_broken_handler_needs_attention_without_its_text(self):
        class Broken:
            def accept(self, values):
                raise RuntimeError("token " + values["token"])

            def start(self, request):
                return {"state": "ready"}
        self.handlers["keyed"] = Broken()
        with self.assertLogs("qorqut.extension_setup", logging.WARNING) as logs:
            entry = self.setups.accept("keyed", {"token": "canary-in-the-error"})
        self.assertEqual((entry.status, entry.note), ("needs_attention", HANDLER_FAILED))
        self.assertNotIn("canary-in-the-error", "\n".join(logs.output))
        self.assertEqual(self.setups.start("keyed", OWNER).status, "needs_attention")   # not a SetupStatus

    def test_a_note_or_refusal_that_quotes_a_value_is_screened(self):
        def quoting(values):
            raise DomainError("validation_error", f"The token {values['token']} is wrong")
        self.handlers["keyed"] = keyed.KeySetup(self.folder, keyed.FIELDS, quoting)
        entry = self.setups.accept("keyed", {"token": "canary-in-the-note"})
        self.assertEqual((entry.status, entry.note), ("needs_attention", CHECK_FAILED))

        class Refusing:
            def accept(self, values):
                raise DomainError("validation_error", "No to " + values["token"])
        self.handlers["keyed"] = Refusing()
        with self.assertRaises(DomainError) as refusal:
            self.setups.accept("keyed", {"token": "canary-in-the-refusal"})
        self.assertEqual((refusal.exception.code, refusal.exception.message), ("validation_error", CHECK_FAILED))
        self.assertEqual(self.found.get("keyed").note, CHECK_FAILED)

    def test_a_handler_that_saves_and_then_raises(self):
        real = keyed.KeySetup(self.folder, keyed.FIELDS, keyed.check)

        class SavesThenRaises:
            def accept(self, values):
                real.accept(values)
                raise RuntimeError("after saving " + values["token"])
        self.handlers["keyed"] = SavesThenRaises()
        with self.assertLogs("qorqut.extension_setup", logging.WARNING) as logs:
            entry = self.setups.accept("keyed", {"token": keyed.TEST_TOKEN})
        # The failure wins: not ready, nothing in effect, and the token is in no log, note or other file.
        self.assertEqual((entry.status, entry.note, self.found.on_for(ORDUBASHY)), ("needs_attention", HANDLER_FAILED, ()))
        self.assertNotIn(keyed.TEST_TOKEN, "\n".join(logs.output))
        self.assertEqual(self.files_holding(keyed.TEST_TOKEN), ["instance/private/extensions/keyed/setup.json"])
        # Disconnecting removes what it saved.
        self.handlers["keyed"] = real
        self.assertEqual(self.setups.disconnect("keyed").status, "not_set_up")
        self.assertEqual(self.files_holding(keyed.TEST_TOKEN), [])

    def test_no_code_loaded(self):
        del self.handlers["keyed"]
        with self.assertRaises(DomainError) as refusal:
            self.setups.start("keyed", OWNER)
        self.assertEqual((refusal.exception.code, refusal.exception.message), ("operation_unavailable", NOT_LOADED))
        self.assertIsNone(self.setups.image("keyed"))

    def test_kind_none_has_nothing_to_do(self):
        def never(ext_id):
            raise AssertionError("no handler is asked for an extension without set-up")
        setups = ExtensionSetups(self.found, never)
        for entry in (setups.start("network", OWNER), setups.accept("network", {}), setups.cancel("network"),
                      setups.disconnect("network")):
            self.assertEqual(entry.status, "ready")
        self.assertIsNone(setups.image("network"))

    def test_an_extension_that_cannot_run_here_is_not_set_up(self):
        with self.assertRaises(DomainError) as refusal:
            ExtensionSetups(self.start(platform="plan9"), self.handlers.get).start("keyed", OWNER)
        self.assertEqual(refusal.exception.code, "invalid_transition")
        with self.assertRaises(DomainError) as refusal:
            self.setups.start("missing", OWNER)
        self.assertEqual(refusal.exception.code, "not_found")
        self.assertEqual(self.spy.calls, [])

    def test_the_image_comes_only_from_the_handler(self):
        self.assertIsNone(self.setups.image("keyed"))
        self.assertEqual(self.spy.calls, ["image"])

    def test_only_a_png_is_served_as_the_image(self):
        png = b"\x89PNG\r\n\x1a\n" + b"rest"
        for given, served in ((png, png), (b"GIF89a....", None), (b"<svg/>", None), ("text", None)):
            self.handlers["keyed"] = SimpleNamespace(image=lambda given=given: given)
            self.assertEqual(self.setups.image("keyed"), served)


class ConnectionsTest(KeyedCase):
    def test_the_card_entry(self):
        self.place_keyed()
        found = self.start()
        [entry] = self.read(lambda connection: extension_connections.items(found, connection))
        self.assertEqual(entry, {
            "id": "keyed", "kind": "contract", "name": "Keyed",
            "summary": "A test extension that is set up with a token and then tells its agents whether the token is saved.",
            "version": "1.0.0", "delivery": "downloadable", "status": "not_set_up", "note": None, "can_set_up": True,
            "setup": {"kind": "key", "fields": [{"id": "token", "label": "Token", "secret": True}]},
            # The Learning Applier's role is not allowed, so it has no row.
            "agents": [{"agent_id": ORDUBASHY, "name": "ordubashy", "role": "ordubashy", "on": True, "in_effect": False}],
            # Without the download tools (no runtime given) the owner's downloads are empty.
            "offers": [], "result": None, "failed": False, "restarting": False})
        self.write(lambda uow: found.set_status("keyed", "ready", uow=uow))
        [entry] = self.read(lambda connection: extension_connections.items(found, connection))
        self.assertEqual(entry["agents"][0]["in_effect"], True)
        self.write(lambda uow: found.switch("keyed", ORDUBASHY, False, uow=uow))
        [entry] = self.read(lambda connection: extension_connections.items(found, connection))
        self.assertEqual((entry["agents"][0]["on"], entry["agents"][0]["in_effect"]), (False, False))

    def test_an_agent_is_named_as_the_dashboard_shows_it(self):
        self.place_keyed()
        found = self.start()
        self.write(lambda uow: uow.connection.execute(
            "UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
            (json.dumps({"role": {"id": "ordubashy"}, "display_name": "Ordubashy"}), ORDUBASHY)))
        [entry] = self.read(lambda connection: extension_connections.items(found, connection))
        self.assertEqual([agent["name"] for agent in entry["agents"]], ["Ordubashy"])

    def test_set_up_is_offered_only_when_it_can_work(self):
        # A code part refused at start: the note says why, and set-up cannot cure it.
        self.place_keyed()
        found = self.start()
        found.refuse_code("keyed", "Its code could not be loaded: its tool names must start with 'keyed_'.")
        [entry] = self.read(lambda connection: extension_connections.items(found, connection))
        self.assertEqual((entry["status"], entry["can_set_up"]), ("needs_attention", False))
        self.assertIsNotNone(entry["setup"])
        shutil.rmtree(self.downloaded / "keyed")
        # A folder the checker refused whose manifest is still read.
        self.place_keyed(lambda data: data.update(version="1.0"))
        found = self.start()
        [entry] = self.read(lambda connection: extension_connections.items(found, connection))
        self.assertEqual((entry["status"], entry["can_set_up"]), ("needs_attention", False))

    def test_the_connectors_view_adds_contract_entries_after_todays(self):
        runtime = SimpleNamespace(selection=SimpleNamespace(extensions=()), database=self.database,
                                  extension_registry=self.start())
        today = connectors.view(runtime)
        self.assertEqual([item["id"] for item in today["connectors"]], ["openrouter", "treg", "whatsapp"])
        self.assertEqual(today, connectors.view(SimpleNamespace(selection=SimpleNamespace(extensions=()))))
        # A folder named like one of today's extensions whose manifest cannot be read leaves today's card.
        broken = self.downloaded / "treg"
        broken.mkdir(parents=True)
        (broken / "extension.yaml").write_text("contract: [\n")
        runtime.extension_registry = self.start()
        self.assertEqual(runtime.extension_registry.get("treg").status, "needs_attention")
        self.assertEqual(connectors.view(runtime), today)
        shutil.rmtree(broken)
        self.place_keyed()
        # A converted extension shows once, by its contract entry.
        treg = self.place(name="treg")
        self.manifest(treg, lambda data: data.update(id="treg", name="treg"))
        skills = treg / "plugin" / "skills"
        (skills / "network").rename(skills / "treg")
        skill = skills / "treg" / "SKILL.md"
        skill.write_text(skill.read_text().replace("qorqut-plugin-network", "qorqut-plugin-treg"))
        runtime.extension_registry = self.start()
        self.assertEqual(runtime.extension_registry.get("treg").refused_findings, ())
        items = connectors.view(runtime)["connectors"]
        self.assertEqual([(item["id"], item["kind"]) for item in items],
                         [("openrouter", "extension"), ("whatsapp", "extension"), ("keyed", "contract"),
                          ("treg", "contract")])

    def test_actions_are_checked_for_their_shape(self):
        parse = extension_connections.parse_action
        self.assertEqual(parse({"action": "start"}), {"action": "start"})
        self.assertEqual(parse({"action": "accept", "values": {"token": "t"}}), {"action": "accept", "values": {"token": "t"}})
        self.assertEqual(parse({"action": "switch", "agent_id": "a", "on": False}),
                         {"action": "switch", "agent_id": "a", "on": False})
        for body in ({"action": "install"}, {"action": "start", "values": {}}, {"action": "accept", "values": {"t": 5}},
                     {"action": "accept"}, {"action": "switch", "agent_id": "a", "on": 1}, [], {"action": "cancel", "x": 1}):
            with self.assertRaises(DomainError):
                parse(body)
        with self.assertRaises(DomainError) as refusal:
            parse({"action": "accept", "values": {"token": 5, "canary-value": "x"}})
        self.assertNotIn("canary-value", refusal.exception.message)


class FreshModuleCase(KeyedCase):
    """The example's module imported afresh from the placed copy."""

    def setUp(self):
        super().setUp()
        path, modules = list(sys.path), {name: module for name, module in sys.modules.items()
                                         if name.split(".")[0] in ("qorqut_keyed", "qorqut_second")}
        for name in modules:
            del sys.modules[name]
        # The example's own folder (put on the path for the other tests) would otherwise give the module first.
        sys.path[:] = [entry for entry in sys.path if entry != str(KEYED / "code")]

        def restore():
            sys.path[:] = path
            for name in [name for name in sys.modules if name.split(".")[0] in ("qorqut_keyed", "qorqut_second")]:
                del sys.modules[name]
            sys.modules.update(modules)
        self.addCleanup(restore)

    def load(self, **arguments):
        from qorqut import extension_registry
        from qorqut.extensions import NO_EXTENSIONS
        return extension_registry.load_code(self.discover(**arguments), NO_EXTENSIONS)


class LoadCodeTest(FreshModuleCase):
    """``load_code`` on found folders."""

    def test_a_folder_without_code_loads_nothing(self):
        from qorqut.extensions import NO_EXTENSIONS
        self.place()
        found = self.discover()
        path = list(sys.path)
        from qorqut import extension_registry
        loaded = extension_registry.load_code(found, NO_EXTENSIONS)
        self.assertEqual(loaded, (found, NO_EXTENSIONS, {}))
        self.assertIs(loaded[1], NO_EXTENSIONS)
        self.assertEqual(sys.path, path)

    def test_the_code_part_joins_the_selection_and_runs_only_its_factory(self):
        folder = self.place_keyed()
        found, selection, loaded = self.load()
        self.assertEqual((selection.ids, list(loaded)), (("keyed",), ["keyed"]))
        self.assertEqual(found[0].findings, ())
        self.assertEqual(sys.path[-1], str(folder.resolve() / "code"))
        made = loaded["keyed"].extension
        self.assertEqual((made.handler, made.ticker.running()), (None, False))   # not installed, nothing started
        self.assertEqual([entry.name for entry in loaded["keyed"].entries], ["keyed_status"])

    def test_declared_tables_are_judged_against_the_store_s_record(self):
        from qorqut.composition.extension_tables import contribution_of, record_contribution, row_of
        from qorqut.extensions import NO_EXTENSIONS
        from qorqut_operations.storage.assembly import StorageContribution
        statement = "CREATE TABLE keyed_items (id TEXT PRIMARY KEY)"
        folder = self.place_keyed()
        code = folder / "code" / "qorqut_keyed" / "extension.py"
        code.write_text(code.read_text() + "\nfrom qorqut_operations.storage.assembly import StorageContribution\n"
                        f"Keyed.tables = lambda self: (StorageContribution('keyed', schema=({statement!r},)),)\n")
        importlib.invalidate_caches()
        from qorqut import extension_registry
        row = row_of("keyed", "1.0.0", (StorageContribution("keyed", schema=(statement,)),))
        found, selection, loaded = extension_registry.load_code(self.discover(), NO_EXTENSIONS)
        # Not in the record yet: the row is made at this start.
        self.assertEqual((selection.ids, loaded["keyed"].tables), (("keyed",), row))
        for name in [name for name in sys.modules if name.split(".")[0] == "qorqut_keyed"]:
            del sys.modules[name]
        # Already recorded: nothing to make.
        found, selection, loaded = extension_registry.load_code(
            self.discover(), NO_EXTENSIONS, recorded=(record_contribution(), contribution_of(row)))
        self.assertEqual((selection.ids, loaded["keyed"].tables), (("keyed",), None))

    def test_a_folder_that_is_refused_or_cannot_run_here_is_not_loaded(self):
        self.place_keyed(lambda data: data.update(platforms=["mac"]))
        found, selection, loaded = self.load()
        self.assertEqual((selection.ids, loaded, found[0].findings), ((), {}, ()))
        self.assertNotIn("qorqut_keyed", sys.modules)

    def test_code_that_cannot_be_loaded_refuses_the_folder(self):
        from qorqut.extension_registry import NO_TABLES, NOT_YET, TABLES_NOT_MADE
        tables = ("\nfrom qorqut_operations.storage.assembly import StorageContribution\n"
                  "Keyed.tables = lambda self: (StorageContribution('keyed', schema=({!r},)),)\n")
        on_instance = ("\n_create = create\ndef create(config=None):\n    made = _create(config)\n"
                       "    made.{} = lambda: ('probe',)\n    return made\n")
        catalog = "\nKeyed.catalog = lambda self: (CatalogContribution(ID, entries=(CatalogEntry({!r}, False, 'read', EXPOSURE),)),)\n"
        cases = (("\nKeyed.storage = lambda self: ('a table',)\n", NO_TABLES),
                 (catalog.format("settings_get"), "Its code could not be loaded: its tool names must start with 'keyed_'."),
                 (catalog.format("undeclared_probe"),
                  "Its code could not be loaded: its tool names must start with 'keyed_'."),
                 ("\nKeyed.catalog = lambda self: 1 / 0\n", "Its code could not be loaded: ZeroDivisionError."),
                 ("\nKeyed.owner_routes = lambda self: ()\n", NOT_YET.format("owner_routes")),
                 # The object is judged as the selection will call it: a part set on the instance counts too.
                 (on_instance.format("global_capabilities"), NOT_YET.format("global_capabilities")),
                 (on_instance.format("storage"), NO_TABLES),
                 ("\nKeyed.tables = lambda self: 1 / 0\n", "Its code could not be loaded: ZeroDivisionError."),
                 (tables.format("CREATE TABLE other_items (id TEXT)"), "Its table other_items is not named keyed_…"),
                 (tables.format("ATTACH DATABASE 'x.sqlite3' AS other"), TABLES_NOT_MADE),
                 ("\ndef create(config=None):\n    raise RuntimeError('broken ' + TEST_TOKEN)\n",
                  "Its code could not be loaded: RuntimeError."),
                 ("\nclass Other(Keyed):\n    id = 'other'\n\ndef create(config=None):\n    return Other()\n",
                  "Its code could not be loaded: its factory does not make the extension 'keyed'."))
        for addition, finding in cases:
            with self.subTest(finding=finding):
                for name in [name for name in sys.modules if name.split(".")[0] == "qorqut_keyed"]:
                    del sys.modules[name]
                folder = self.place_keyed()
                code = folder / "code" / "qorqut_keyed" / "extension.py"
                code.write_text(code.read_text() + addition)
                importlib.invalidate_caches()
                path, modules = list(sys.path), set(sys.modules)
                with self.assertLogs("qorqut.extension_registry", logging.WARNING):
                    found, selection, loaded = self.load()
                self.assertEqual((selection.ids, loaded, found[0].findings), ((), {}, (finding,)))
                # A refused folder leaves the import path and the imported modules as they were.
                self.assertEqual(sys.path, path)
                self.assertEqual({name for name in sys.modules if name.split(".")[0] == "qorqut_keyed"}, set())
                self.assertLessEqual(set(sys.modules) - modules, {name for name in sys.modules
                                                                  if not name.startswith("qorqut_keyed")})
                shutil.rmtree(folder)

    def test_a_module_of_the_same_name_from_elsewhere_is_refused(self):
        sys.path.insert(0, str(KEYED / "code"))
        import qorqut_keyed.extension  # noqa: F401 - the module name is now taken by another folder
        self.place_keyed()
        with self.assertLogs("qorqut.extension_registry", logging.WARNING):
            found, selection, loaded = self.load()
        self.assertEqual((selection.ids, loaded), ((), {}))
        self.assertIn("is not the one in its code folder", found[0].findings[0])


class InstallCodeTest(FreshModuleCase):
    """``install_code``: a loaded code part is installed only when it registers exactly the tools it declared, none of
    them Qorqbot's; otherwise nothing of it is registered and the folder needs attention."""

    def install(self, addition=""):
        from qorqut import extension_registry
        from qorqut.composition.catalog import builtin_registry
        folder = self.place_keyed()
        code = folder / "code" / "qorqut_keyed" / "extension.py"
        code.write_text(code.read_text() + addition)
        importlib.invalidate_caches()
        path = list(sys.path)
        found, selection, loaded = self.load()
        self.assertEqual(list(loaded), ["keyed"])
        self.registry = extension_registry.ExtensionRegistry(found, changes=self.feed, code=loaded)
        self.registry.sync(self.database, self.layer)
        operations = builtin_registry()
        self.core = {descriptor.name for descriptor in operations.installed()}
        runtime = SimpleNamespace(extension_private=lambda ext_id: PrivateFolder(self.paths.instance_root, ext_id),
                                  registry=operations)
        extension_registry.install_code(runtime, self.registry, operations)
        return operations, path

    def test_a_fine_code_part_registers_its_tool_and_it_is_routed_to_it(self):
        operations, _ = self.install()
        self.assertEqual(operations.get("keyed_status").name, "keyed_status")
        self.assertEqual((self.registry.extension_of("keyed_status"), self.registry.get("keyed").status),
                         ("keyed", "not_set_up"))

    def test_an_install_may_read_the_registry(self):
        reading = ("\n_install = Keyed.install\n"
                   "def install(self, runtime):\n"
                   "    assert runtime.registry.known('operations_batch') and not runtime.registry.known(STATUS_TOOL)\n"
                   "    _install(self, runtime)\n"
                   "Keyed.install = install\n")
        operations, _ = self.install(reading)
        self.assertEqual((self.registry.extension_of("keyed_status"), self.registry.get("keyed").status),
                         ("keyed", "not_set_up"))

    def test_a_second_folder_that_faults_leaves_the_first_whole(self):
        from qorqut import extension_registry
        from qorqut.composition.catalog import builtin_registry
        cases = (("\nKeyed.catalog = lambda self: 1 / 0\n", False),
                 ("\ndef install(self, runtime):\n    raise RuntimeError('boom')\nKeyed.install = install\n", True))
        for addition, loads in cases:
            with self.subTest(loads=loads):
                for name in [name for name in sys.modules if name.split(".")[0] in ("qorqut_keyed", "qorqut_second")]:
                    del sys.modules[name]
                self.place_keyed()
                self.place_renamed("second", addition)
                with self.assertLogs("qorqut.extension_registry", logging.WARNING):
                    found, selection, loaded = self.load()
                    self.assertEqual(sorted(loaded), ["keyed", "second"] if loads else ["keyed"])
                    self.registry = extension_registry.ExtensionRegistry(found, changes=self.feed, code=loaded)
                    self.registry.sync(self.database, self.layer)
                    operations = builtin_registry()
                    runtime = SimpleNamespace(registry=operations, extension_private=lambda ext_id: PrivateFolder(
                        self.paths.instance_root, ext_id))
                    extension_registry.install_code(runtime, self.registry, operations)
                self.assertEqual(list(self.registry.code), ["keyed"])
                self.assertEqual((operations.get("keyed_status").name, self.registry.extension_of("keyed_status"),
                                  self.registry.get("keyed").status), ("keyed_status", "keyed", "not_set_up"))
                self.assertIsNotNone(self.registry.setup_handler("keyed"))
                self.assertIs(self.registry.code["keyed"].extension.handler, self.registry.setup_handler("keyed"))
                self.assertEqual((self.registry.get("second").status, operations.known("second_status"),
                                  self.registry.extension_of("second_status")), ("needs_attention", False, None))
                shutil.rmtree(self.downloaded / "keyed")
                shutil.rmtree(self.downloaded / "second")

    def test_a_fault_registers_nothing_and_needs_attention(self):
        register = ("\n_install = Keyed.install\n"
                    "def install(self, runtime):\n    _install(self, runtime)\n"
                    "    runtime.registry.register(OperationDescriptor({!r}, schema(), effect_kind='read', "
                    "supports_signoff=False, scope_kinds={{'global'}}, exposure=EXPOSURE, "
                    "resolve_namespace=lambda ctx, args: 'global', global_capability='read_global_context', "
                    "default_mode='allow', description='x', read=lambda ctx, args, options, uow: {{}}))\n"
                    "Keyed.install = install\n")
        cases = (("\ndef install(self, runtime):\n    raise RuntimeError('boom ' + TEST_TOKEN)\nKeyed.install = install\n",
                  "Its code could not be loaded: RuntimeError."),
                 (register.format("undeclared_probe"),
                  "Its code could not be loaded: the tools it registers are not the tools it declares."),
                 (register.format("settings_get"),
                  "Its code could not be loaded: the tools it registers are not the tools it declares."),
                 ("\nKeyed.install = lambda self, runtime: None\n",
                  "Its code could not be loaded: the tools it registers are not the tools it declares."))
        for addition, finding in cases:
            with self.subTest(finding=finding):
                for name in [name for name in sys.modules if name.split(".")[0] == "qorqut_keyed"]:
                    del sys.modules[name]
                with self.assertLogs("qorqut.extension_registry", logging.WARNING) as logs:
                    operations, path = self.install(addition)
                self.assertNotIn(keyed.TEST_TOKEN, "\n".join(logs.output))
                entry = self.registry.get("keyed")
                self.assertEqual((entry.status, entry.note), ("needs_attention", finding))
                self.assertEqual({descriptor.name for descriptor in operations.installed()}, self.core)
                self.assertFalse(operations.known("keyed_status") or operations.known("undeclared_probe"))
                self.assertEqual((self.registry.code, self.registry.extension_of("keyed_status"),
                                  self.registry.extension_of("settings_get")), ({}, None, None))
                self.assertEqual(sys.path, path)
                self.assertEqual([name for name in sys.modules if name.split(".")[0] == "qorqut_keyed"], [])
                self.assertEqual(self.registry.setup_handler("keyed"), None)
                shutil.rmtree(self.downloaded / "keyed")

    def test_a_tool_qorqbot_already_has_is_refused(self):
        from qorqut import extension_registry
        from qorqut.composition.catalog import builtin_registry
        from qorqut_operations.application import OperationDescriptor
        from qorqut_operations.application.plans import schema
        self.place_keyed()
        found, selection, loaded = self.load()
        self.registry = extension_registry.ExtensionRegistry(found, code=loaded)
        operations = builtin_registry()
        core = OperationDescriptor("keyed_status", schema(), effect_kind="read", supports_signoff=False,
                                   scope_kinds={"global"}, exposure=frozenset({"mcp"}), description="core",
                                   resolve_namespace=lambda ctx, args: "global", global_capability="read_global_context",
                                   read=lambda ctx, args, options, uow: {"core": True})
        operations.register(core)
        runtime = SimpleNamespace(extension_private=lambda ext_id: PrivateFolder(self.paths.instance_root, ext_id),
                                  registry=operations)
        with self.assertLogs("qorqut.extension_registry", logging.WARNING):
            extension_registry.install_code(runtime, self.registry, operations)
        self.assertIs(operations.get("keyed_status"), core)
        self.assertIsNone(self.registry.extension_of("keyed_status"))
        self.assertEqual(self.registry._findings(self.registry._found("keyed")),
                         ("Its code could not be loaded: it declares a tool Qorqbot already has.",))


class FakeService:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def stop(self, timeout=None):
        self.events.append("stop")


class GatedServiceTest(KeyedCase):
    def setUp(self):
        super().setUp()
        self.place_keyed()
        self.found = self.start()
        self.inner = FakeService()
        self.gate = GatedService("keyed", ServiceDeclaration("keyed_ticker", self.inner, "background", "background"),
                                 self.found, schedule=lambda work: work())
        self.step = self.gate.declaration()

    def status(self, status):
        self.write(lambda uow: self.found.set_status("keyed", status, uow=uow))

    def test_the_declaration_keeps_the_anchors(self):
        self.assertEqual((self.step.name, self.step.service, self.step.start_before, self.step.stop_before),
                         ("keyed_ticker", self.gate, "background", "background"))

    def test_runs_only_while_ready_and_in_effect_for_an_agent(self):
        self.assertFalse(self.gate.running())
        self.step.start(self.step.service)
        # Started by the lifetime, so the barrier resumes it, while the inner service waits.
        self.assertEqual((self.inner.events, self.gate.running()), ([], True))
        self.status("setting_up")
        self.assertEqual(self.inner.events, [])
        self.status("ready")                     # in effect for Ordubashy by ``on_after_setup``
        self.assertEqual(self.inner.events, ["start"])
        self.write(lambda uow: self.found.switch("keyed", ORDUBASHY, False, uow=uow))
        self.assertEqual(self.inner.events[1:], ["stop"])
        worker = "agent-worker"
        self.write(lambda uow: uow.connection.execute(
            "INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES(?,'agent','w','now','now')", (worker,)))
        self.write(lambda uow: uow.connection.execute(
            "INSERT INTO agent_profiles(agent_id,principal_id,name,workspace,profile_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'now','now')", (worker, worker, "worker", "/w/worker", json.dumps({"role": {"id": "global-worker"}}))))
        self.write(lambda uow: self.found.switch("keyed", worker, True, uow=uow))
        self.assertEqual(self.inner.events[2:], ["start"])
        self.status("needs_attention")
        self.assertEqual(self.inner.events[3:], ["stop"])

    def test_the_barrier_stops_and_resumes_it(self):
        self.status("ready")
        self.step.start(self.step.service)
        proof = self.step.stop(self.step.name, self.step.service, 1)
        self.assertEqual((self.inner.events, proof), (["start", "stop"], stopping.drained("keyed_ticker")))
        # Stopped by the lifetime: changes start nothing.
        self.write(lambda uow: self.found.switch("keyed", APPLIER, False, uow=uow))
        self.status("needs_attention")
        self.status("ready")
        self.assertEqual(self.inner.events, ["start", "stop"])
        self.step.start(self.step.service)
        self.assertEqual(self.inner.events, ["start", "stop", "start"])
        # A dormant step stops with its own proof and touches nothing.
        self.status("not_set_up")
        self.assertEqual(self.step.stop(self.step.name, self.step.service, 1), stopping.drained("keyed_ticker"))
        self.assertEqual(self.inner.events, ["start", "stop", "start", "stop"])

    def barrier(self):
        """The recovery barrier's writer for the step, built as ``Runtime.sync_recovery_writers`` builds it."""
        step = self.step
        return BoundedWriter(step.service, step.name, stop=lambda service, timeout: step.stop(step.name, service, timeout),
                             running=step.running, start=step.start)

    def test_a_dormant_step_comes_back_after_the_real_barrier(self):
        self.step.start(self.step.service)
        writer = self.barrier()
        self.assertEqual(writer.drain(timeout=1).state, "drained")
        writer.resume()
        self.assertEqual(self.inner.events, [])
        self.status("ready")                     # set up after the barrier
        self.assertEqual(self.inner.events, ["start"])
        writer.drain(timeout=1)
        self.assertEqual(self.inner.events, ["start", "stop"])
        writer.resume()
        self.assertEqual(self.inner.events, ["start", "stop", "start"])

    def test_a_failing_start_is_logged_and_tried_again(self):
        self.status("ready")
        failing = [True]

        def start(service):
            if failing[0]:
                raise RuntimeError("no network")
            service.start()
        gate = GatedService("keyed", ServiceDeclaration("keyed_ticker", self.inner, "background", "background",
                                                        start=start), self.found, schedule=lambda work: work())
        step = gate.declaration()
        with self.assertLogs("qorqut.extension_setup", logging.WARNING):
            step.start(step.service)             # Qorqbot's start goes on
        self.assertEqual((self.inner.events, gate._running, step.running(step.service)), ([], False, True))
        failing[0] = False
        self.write(lambda uow: self.found.set_status("keyed", "ready", "Checked again.", uow=uow))
        self.assertEqual(self.inner.events, ["start"])

    def test_a_stop_without_a_drained_proof_still_runs(self):
        proofs = iter(["running", "drained"])
        gate = GatedService("keyed", ServiceDeclaration(
            "keyed_ticker", self.inner, "background", "background",
            stop=lambda name, service, timeout: SimpleNamespace(state=next(proofs))), self.found, schedule=lambda work: work())
        step = gate.declaration()
        step.start(step.service)
        self.status("ready")
        self.status("needs_attention")
        self.assertTrue(gate._running)
        self.status("not_set_up")               # the next check stops it again
        self.assertFalse(gate._running)

    def test_an_inner_service_that_ended_by_itself_is_started_at_the_next_check(self):
        alive = [True]
        gate = GatedService("keyed", ServiceDeclaration("keyed_ticker", self.inner, "background", "background",
                                                        running=lambda service: alive[0]),
                            self.found, schedule=lambda work: work())
        step = gate.declaration()
        step.start(step.service)
        self.status("ready")
        self.assertEqual(self.inner.events, ["start"])
        alive[0] = False
        self.write(lambda uow: self.found.set_status("keyed", "ready", "Checked again.", uow=uow))
        self.assertEqual(self.inner.events, ["start", "start"])

    def test_changing_the_role_of_the_agent_it_is_on_for_stops_it(self):
        gate = GatedService("keyed", ServiceDeclaration("keyed_ticker", self.inner, "background", "background"),
                            self.found, changes=self.feed, schedule=lambda work: work())
        step = gate.declaration()
        step.start(step.service)
        self.status("ready")
        self.assertEqual(self.inner.events, ["start"])
        # The profile write touches no registry row; the change feed reports the agent profiles table.
        self.write(lambda uow: uow.connection.execute(
            "UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
            (json.dumps({"role": {"id": "learning-applier"}}), ORDUBASHY)))
        self.assertEqual(self.inner.events, ["start", "stop"])

    def test_the_example_ticker_counts_only_while_it_may(self):
        extension = keyed.Keyed(interval=0.01)
        [declaration] = extension.services()
        gate = GatedService("keyed", declaration, self.found, schedule=lambda work: work())
        step = gate.declaration()
        step.start(step.service)
        self.assertFalse(extension.ticker.running())
        self.status("ready")
        self.assertTrue(extension.ticker.running())
        deadline = time.monotonic() + 5
        while extension.ticker.ticks < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(extension.ticker.ticks, 2)
        self.status("needs_attention")
        self.assertTrue(step.running(step.service))         # the lifetime still has the step started
        self.assertFalse(extension.ticker.running())
        counted = extension.ticker.ticks
        time.sleep(0.1)                                     # ten intervals
        self.assertEqual(extension.ticker.ticks, counted)
        self.assertEqual(step.stop(step.name, step.service, 1), stopping.drained("keyed_ticker"))
        self.assertFalse(step.running(step.service))

    def test_the_example_ticker_stop_refuses_while_its_thread_runs(self):
        ticker = keyed.Ticker(0.01)
        ticker.start()
        ticker._thread = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: True)
        with self.assertRaises(TimeoutError):
            stopping.void(lambda service, timeout: service.stop(timeout=timeout))("keyed_ticker", ticker, 0.01)

    def test_the_example_reads_nothing_until_set_up(self):
        extension = keyed.create()
        self.assertIsNone(extension.setup_handler())
        runtime = SimpleNamespace(extension_private=lambda ext_id: PrivateFolder(self.paths.instance_root, ext_id),
                                  registry=SimpleNamespace(register=lambda descriptor: None))
        extension.install(runtime)
        self.assertEqual(extension.status(), {"token_saved": False})
        self.assertEqual(extension.setup_handler().status(), SetupStatus("not_set_up"))
        self.assertFalse((self.paths.instance_root / "private").exists())


if __name__ == "__main__":
    unittest.main()
