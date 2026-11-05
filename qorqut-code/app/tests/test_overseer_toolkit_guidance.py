"""The installed packaged Overseer guidance names executable interfaces only.

A fresh synthetic installation seeds the real module package; these checks read
that installed content and validate its operation vocabulary against the actual
registries. They do not assert prose or model behavior.
"""
import re

import pytest

from qorqut_agentic.cli import OPERATIONS
from host_foundation_support import build_instance


PATHS = ("agentic.yaml", "instructions/extract.md", "instructions/review.md", "instructions/apply.md",
         "instructions/record-direct-instruction.md", "workflows/extract.yaml", "workflows/review.yaml",
         "workflows/apply.yaml")


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    instance, _ = build_instance(tmp_path_factory.mktemp("installed"))
    view = instance.runtime.agentic_view
    text = "\n".join(view.layer.catalog.read_resource("overseer", path)["content"] for path in PATHS)
    return instance, text


def test_installed_guidance_names_only_registered_host_operations(installed):
    instance, text = installed
    used = set(re.findall(r"\boverseer_[a-z_]+\b", text))
    assert used
    unknown = sorted(name for name in used if not instance.runtime.registry.known(name))
    assert not unknown, unknown
    for named in ("overseer_application_prepare", "overseer_application_prepare_merge", "overseer_application_publish"):
        assert named in used and instance.runtime.registry.known(named)


def test_installed_guidance_names_only_supported_portable_operations(installed):
    _, text = installed
    used = set(re.findall(r"\boverseer\.[a-z_]+\.[a-z_]+\b", text))
    assert used
    assert used <= set(OPERATIONS), sorted(used - set(OPERATIONS))
    for invented in ("overseer.applications.prepare", "overseer.applications.publish"):
        assert invented not in text