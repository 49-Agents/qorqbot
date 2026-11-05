"""Shared setup for the Fastmail extension tests.

Host-composed cases use the host's own ``host_foundation_support``; the host checkout's test helpers are put on the import path
from ``QORQUT_HOST_TESTS`` or, by default, the sibling ``qorqut-code/app/tests`` of this workspace. Everything runs on
disposable data under ``tmp_path`` with an in-process fake engine transport: no network, credential, mail account, native
session or operational data is touched."""

import os
import sys
from pathlib import Path

HOST_TESTS = Path(os.environ.get("QORQUT_HOST_TESTS") or Path(__file__).resolve().parents[2] / "qorqut-code" / "app" / "tests")
if (HOST_TESTS / "host_foundation_support.py").is_file() and str(HOST_TESTS) not in sys.path:
    sys.path.insert(0, str(HOST_TESTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
