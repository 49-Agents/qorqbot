"""The Viewbox placement tool is retired: the name stays declared, nothing handles it, its guidance is never composed."""
from pathlib import Path
import unittest

from qorqut_agentic.behavior import composition
from qorqut_operations.contracts import DomainError

from qorqut.catalog.packages import validate_policy
from qorqut.composition.catalog import builtin_registry
from qorqut.domains.viewbox import AgentViewbox


class ViewboxToolRetiredTests(unittest.TestCase):
    def test_name_stays_declared_but_has_no_handler(self):
        registry = builtin_registry()
        self.assertTrue(registry.known("viewbox_place"))
        AgentViewbox()  # Building the board reader installs nothing.
        self.assertFalse(hasattr(AgentViewbox, "register"))
        with self.assertRaises(DomainError) as raised:
            registry.get("viewbox_place")
        self.assertEqual(raised.exception.code, "operation_unavailable")

    def test_a_role_permission_map_naming_it_is_still_valid(self):
        validate_policy({"viewbox_place": "allow"}, builtin_registry())

    def test_no_module_installs_a_handler(self):
        # The service registry gets its handlers from the domain modules; none may describe the retired name again.
        app = Path(__file__).resolve().parents[1] / "qorqut"
        found = [str(path.relative_to(app)) for path in app.rglob("*.py")
                 if 'OperationDescriptor("viewbox_place"' in path.read_text(encoding="utf-8")
                 or "OperationDescriptor('viewbox_place'" in path.read_text(encoding="utf-8")]
        self.assertEqual(found, [])

    def test_guidance_is_composed_only_with_the_tool(self):
        # Unavailable tools never reach available_operations (resolver.effective_tool_policy), so the guidance drops out.
        self.assertNotIn("viewbox-guidance", composition.capability_guidance(frozenset({"files_link"})))


if __name__ == "__main__":
    unittest.main()
