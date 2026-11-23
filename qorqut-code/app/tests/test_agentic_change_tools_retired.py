"""The per-instruction Signoff tools are retired: the names stay declared, nothing handles them, no instruction names them."""
from pathlib import Path
import unittest

from qorqut_operations.contracts import DomainError

from qorqut.catalog.packages import validate_policy
from qorqut.composition.catalog import builtin_registry

NAMES = ("agentic_change_propose", "agentic_change_get", "agentic_change_reconcile")
ROOT = Path(__file__).resolve().parents[3]


class AgenticChangeToolsRetiredTests(unittest.TestCase):
    def test_names_stay_declared_but_have_no_handler(self):
        registry = builtin_registry()
        for name in NAMES:
            self.assertTrue(registry.known(name))
            with self.assertRaises(DomainError) as raised:
                registry.get(name)
            self.assertEqual(raised.exception.code, "operation_unavailable")

    def test_a_role_permission_map_naming_them_is_still_valid(self):
        validate_policy({name: "allow" for name in NAMES}, builtin_registry())

    def test_no_module_installs_a_handler(self):
        app = ROOT / "qorqut-code" / "app" / "qorqut"
        found = [str(path.relative_to(app)) for path in app.rglob("*.py") if path.name != "catalog.py"
                 and any(f"'{name}'" in path.read_text(encoding="utf-8") or f'"{name}"' in path.read_text(encoding="utf-8")
                         for name in NAMES)]
        self.assertEqual(found, [])

    def test_no_shipped_instruction_names_them(self):
        defaults = ROOT / "qorqut-agentic" / "src" / "qorqut_agentic" / "resources" / "defaults"
        found = [str(path.relative_to(defaults)) for path in defaults.rglob("*") if path.is_file()
                 and any(name in path.read_text(encoding="utf-8", errors="replace") for name in NAMES)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
