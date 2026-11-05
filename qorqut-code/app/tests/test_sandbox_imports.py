"""Standalone application imports must not recursively compose the service."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ("bazarlyk.signoffs", "bazarlyk.background.providers", "bazarlyk.domains"))
def test_application_modules_import_in_a_fresh_interpreter(module):
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
