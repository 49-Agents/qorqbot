"""Synthetic explicit component locations for tests; reads no instance data and creates nothing.

The test workspace is ``QORQUT_TEST_WORKSPACE_ROOT``, or the directory holding this ``qorqut-code`` checkout. It supplies
the instance root and the agentic distribution defaults for roles; ``app`` and the child repositories are the
submodule checkouts of this checkout.
"""

import os
from pathlib import Path

from bazarlyk.instance_paths import InstancePaths

CODE = Path(__file__).resolve().parents[2]


def workspace_root():
    return Path(os.environ.get("QORQUT_TEST_WORKSPACE_ROOT", CODE.parent))


def synthetic_paths(instance_root=None, **components):
    root = Path(instance_root) if instance_root is not None else workspace_root()
    selected = {"app": CODE / "app", "operations_engine": CODE / "qorqut-operations", "context_engine": CODE / "qorqut-context",
                "dashboard": CODE / "biz-dashboard",
                "roles": workspace_root() / "qorqut-agentic/src/qorqut_agentic/resources/defaults", **components}
    return InstancePaths(root, selected)


PATHS = synthetic_paths()
