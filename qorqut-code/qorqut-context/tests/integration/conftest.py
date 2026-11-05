"""Integration tests need the ``integration`` extra (qorqut-operations); without it they are not collected."""

import importlib.util

collect_ignore_glob = [] if importlib.util.find_spec("qorqut_operations") else ["test_*.py"]
