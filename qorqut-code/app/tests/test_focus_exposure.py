"""Focus operations are dashboard-only, and they register against the built-in catalog the way a fresh
install assembles it (the catalog first, then Focus's own handlers)."""
import unittest
from types import SimpleNamespace as NS

from qorqut.composition.catalog import builtin_registry
from qorqut.inbox.focus import FocusService

FOCUS_OPERATIONS = ('focus_get', 'focus_show', 'focus_clear', 'focus_browser_input', 'focus_present',
                    'focus_edit', 'focus_move', 'focus_set_archived', 'focus_reply')


class FocusExposureTest(unittest.TestCase):
    def test_focus_operations_register_dashboard_only(self):
        registry = builtin_registry()
        inbox = NS(app=NS(registry=registry), domains=NS(items=NS(kinds=('task', 'signoff'))))
        FocusService(inbox, None).register()
        for name in FOCUS_OPERATIONS:
            with self.subTest(name=name):
                self.assertEqual(registry._descriptors[name].exposure, frozenset({'dashboard'}))


if __name__ == '__main__':
    unittest.main()
