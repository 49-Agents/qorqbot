"""The set-up interface of contract extensions and the ready-made ``KeySetup`` for set-up kind ``key``: the typed values
go only to the private folder, with owner-only modes, and never into a status, a note, an error or a log line."""
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import traceback
import unittest

from qorqut.extension_contract import SetupField
from qorqut.extension_private import PrivateFolder
from qorqut.extension_setup import CHECK_FAILED, SET_UP_AGAIN, SETUP_FILE, KeySetup
from qorqut.extensions import Extension, SetupStart, SetupStatus
from qorqut_operations.contracts import DomainError

# Today's extensions are bundled source beside the host; they are not installed in the test environment.
EXTENSIONS = Path(__file__).resolve().parents[2] / "extensions"
for name in ("whatsapp", "treg", "openrouter"):
    sys.path.insert(0, str(EXTENSIONS / name))

TOKEN = "canary-token-5b1e0d"
FIELDS = (SetupField("token", "Token", True), SetupField("account", "Account"))
VALUES = {"token": TOKEN, "account": "main"}


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class SetupTypesTest(unittest.TestCase):
    def test_status_states(self):
        for state in ("not_set_up", "setting_up", "ready", "needs_attention"):
            self.assertEqual(SetupStatus(state).state, state)
        for state in ("not_installed", "unsupported", "connected", ""):
            with self.assertRaises(DomainError):
                SetupStatus(state)
        with self.assertRaises(DomainError):
            SetupStatus("ready", image="yes")

    def test_start(self):
        self.assertEqual(SetupStart("owner", return_url="https://host/?connection=keyed").by, "owner")
        self.assertEqual(SetupStart("ordubashy", "agent-1").agent_id, "agent-1")
        for bad in (dict(by="worker"), dict(by="ordubashy"), dict(by="ordubashy", agent_id="a", return_url="https://x")):
            with self.assertRaises(DomainError):
                SetupStart(**bad)

    def test_extensions_have_no_handler_by_default(self):
        self.assertIsNone(Extension().setup_handler())

    def test_todays_extensions_have_no_handler(self):
        from qorqut_openrouter.extension import create as openrouter
        from qorqut_treg.extension import create as treg
        from qorqut_whatsapp.extension import create as whatsapp
        for factory in (whatsapp, treg, openrouter):
            extension = factory()
            self.assertIsNone(extension.setup_handler(), extension.id)
        self.assertIsNone(whatsapp().setup)       # WhatsApp's own attribute keeps its name and meaning


class KeySetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.folder = PrivateFolder(self.root, "keyed")
        self.checked = []
        self.setup = KeySetup(self.folder, FIELDS, self.checked.append)

    def assertNoToken(self, value):
        self.assertNotIn(TOKEN, repr(value))
        if isinstance(value, BaseException):
            self.assertNotIn(TOKEN, "".join(traceback.format_exception(value)))

    def test_not_set_up_creates_nothing(self):
        self.assertEqual(self.setup.status(), SetupStatus("not_set_up"))
        self.assertEqual(self.setup.start(SetupStart("ordubashy", "agent-1")).state, "not_set_up")
        self.assertEqual(self.setup.cancel().state, "not_set_up")
        self.assertIsNone(self.setup.image())
        self.assertIsNone(self.setup.values())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_accept_keeps_values_privately(self):
        with self.assertNoLogs(level=logging.DEBUG):
            status = self.setup.accept(VALUES)
        self.assertEqual(status, SetupStatus("ready"))
        self.assertNoToken(status)
        self.assertEqual(self.checked, [VALUES])
        path = self.folder.path / SETUP_FILE
        self.assertEqual(mode(path), 0o600)
        for directory in (self.root / "private", self.root / "private" / "extensions", self.folder.path):
            self.assertEqual(mode(directory), 0o700)
        self.assertEqual(json.loads(path.read_bytes()), VALUES)
        self.assertEqual(os.listdir(self.folder.path), [SETUP_FILE])
        self.assertEqual(self.setup.values(), VALUES)
        self.assertNoToken(self.setup.status())
        self.assertNoToken(self.setup.start(SetupStart("owner")))

    def test_unknown_or_missing_fields_are_refused_without_the_value(self):
        for values in ({"token": TOKEN}, {"token": TOKEN, "account": ""}, {**VALUES, TOKEN: TOKEN},
                       {"token": TOKEN, "account": 5}, [("token", TOKEN)]):
            with self.assertRaises(DomainError) as caught:
                self.setup.accept(values)
            self.assertEqual(caught.exception.code, "validation_error")
            self.assertNoToken(caught.exception)
        self.assertEqual(self.checked, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_check_needs_attention_and_keeps_nothing_new(self):
        def refuse(values):
            raise DomainError("validation_error", "That does not look like a token. Paste it exactly as shown.")
        status = KeySetup(self.folder, FIELDS, refuse).accept(VALUES)
        self.assertEqual(status, SetupStatus("needs_attention", "That does not look like a token. Paste it exactly as shown."))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_crashing_check_never_shows_the_value(self):
        # A refusal that quotes a value is screened by ``ExtensionSetups.accept`` (test_extension_setups.py).
        def crashing(values):
            raise RuntimeError(f"service said no to {values['token']}")
        status = KeySetup(self.folder, FIELDS, crashing).accept(VALUES)
        self.assertEqual(status, SetupStatus("needs_attention", CHECK_FAILED))
        self.assertNoToken(status)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_check_keeps_the_saved_values(self):
        self.setup.accept(VALUES)
        def refuse(values):
            raise DomainError("validation_error", "Not accepted.")
        self.assertEqual(KeySetup(self.folder, FIELDS, refuse).accept({"token": "other", "account": "x"}).state,
                         "needs_attention")
        self.assertEqual(self.setup.values(), VALUES)

    def test_replace_and_disconnect(self):
        self.setup.accept(VALUES)
        self.setup.accept({"token": "second", "account": "main"})
        self.assertEqual(self.setup.values()["token"], "second")
        self.assertEqual(os.listdir(self.folder.path), [SETUP_FILE])
        self.assertEqual(self.setup.cancel().state, "ready")          # cancel never removes what was saved
        self.assertEqual(self.setup.disconnect(), SetupStatus("not_set_up"))
        self.assertEqual(os.listdir(self.folder.path), [])
        self.assertIsNone(self.setup.values())
        self.assertEqual(self.setup.disconnect().state, "not_set_up")

    def test_check_must_return_none_to_accept(self):
        for answer in (False, True, 0, "ok", {"valid": False}):
            status = KeySetup(self.folder, FIELDS, lambda values, answer=answer: answer).accept(VALUES)
            self.assertEqual(status, SetupStatus("needs_attention", CHECK_FAILED))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(KeySetup(self.folder, FIELDS, lambda values: None).accept(VALUES).state, "ready")

    def assertSetUpAgain(self, setup=None):
        setup = setup or self.setup
        status = setup.status()
        self.assertEqual(status, SetupStatus("needs_attention", SET_UP_AGAIN))
        with self.assertRaises(DomainError) as caught:
            setup.values()
        self.assertNoToken(status)
        self.assertNoToken(caught.exception)

    def test_status_and_values_agree_on_a_file_that_is_not_private(self):
        self.setup.accept(VALUES)
        os.chmod(self.folder.path / SETUP_FILE, 0o644)
        self.assertSetUpAgain()

    def test_status_and_values_agree_on_a_symbolic_link(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(VALUES))
        os.chmod(outside, 0o600)
        self.setup.accept(VALUES)
        os.replace(self.folder.path / SETUP_FILE, self.root / "moved")
        os.symlink(outside, self.folder.path / SETUP_FILE)
        self.assertSetUpAgain()

    def test_status_and_values_agree_on_damaged_content(self):
        for body in (b"not json " + TOKEN.encode(), json.dumps({"token": TOKEN, "account": 5}).encode(),
                     json.dumps([TOKEN]).encode()):
            self.folder.write(SETUP_FILE, body)
            self.assertSetUpAgain()

    def test_changed_fields_ask_for_set_up_again(self):
        self.setup.accept(VALUES)
        added = KeySetup(self.folder, (*FIELDS, SetupField("region", "Region")), self.checked.append)
        dropped = KeySetup(self.folder, FIELDS[:1], self.checked.append)
        for setup in (added, dropped):
            self.assertSetUpAgain(setup)
        self.assertEqual(added.accept({**VALUES, "region": "eu"}).state, "ready")

    def test_construction(self):
        for fields in ((), (SetupField("token", "Token"), SetupField("token", "Again"))):
            with self.assertRaises(DomainError):
                KeySetup(self.folder, fields, self.checked.append)
        with self.assertRaises(DomainError):
            KeySetup(self.folder, FIELDS, None)
        with self.assertRaises(DomainError):
            KeySetup(self.root, FIELDS, self.checked.append)


if __name__ == "__main__":
    unittest.main()
