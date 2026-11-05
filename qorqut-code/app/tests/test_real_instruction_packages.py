"""Real v3 Email Management package and real roles against this core, unmodified and read-only.

The inputs are this workspace's ``dependencies/email-management`` and ``qorqut-agentic`` defaults;
``QORQUT_REAL_EMAIL_MANAGEMENT`` and ``QORQUT_TEST_ROLE_ROOT`` select alternative sources. The cases skip when a checkout is absent. Optional ``QORQUT_REAL_EMAIL_MANAGEMENT_DIGEST`` and
``QORQUT_REAL_ORDUBASHY_DIGEST`` pin the expected package digests. The packages are only read; every copy the preparation makes
lives inside a pytest ``tmp_path``. Nothing is replaced, filtered or tolerated: every operation a role names must be registered.
No provider, account or network is involved."""

import os
from pathlib import Path
import re

import pytest

from bazarlyk.bootstrap import email_guidance
from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.catalog.packages import load_package
from email_guidance_support import PLACEHOLDER, configure_sources, create_agent, grants, publish_plugin
from host_foundation_support import build_instance, publish_role, register_sources, role_root
from instance_support import workspace_root
from qorqut_operations.contracts import Principal

PACKAGE = Path(os.environ.get("QORQUT_REAL_EMAIL_MANAGEMENT") or workspace_root() / "dependencies" / "email-management")
ROLES = role_root()
pytestmark = pytest.mark.skipif(not ((PACKAGE / "plugin.yaml").is_file() and (ROLES / "roles/imported-roles/native/roles/ordubashy/role.yaml").is_file()),
                                reason="the workspace has no email-management and agentic defaults")

VERSIONS = {"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.156.1"}
SKILL = "bazarlyk-plugin-email-management"
ROLE_IDS = ("ordubashy", "global-worker", "learning-applier")
MAIL_NAME = re.compile(r"\b(?:email|business_email|fastmail|signoffs_email|emails)_[a-z_]+\b")


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("QORQUT_INSTALLATION_CONFIG", raising=False)
    from bazarlyk.bootstrap import diagnostics
    monkeypatch.setattr(diagnostics, "prepared_helpers", lambda *_: {"ready": True})
    configure_sources(monkeypatch, tmp_path, PACKAGE)
    instance, _ = build_instance(tmp_path)
    register_sources(instance, instance.business, "first")
    instance.pin = publish_plugin(instance)
    return instance


def test_the_real_package_is_the_approved_v3_snapshot():
    package = load_package(PACKAGE, "plugin.yaml", "plugin", None)
    assert package.version.split(".")[0] == "3"
    email_guidance.validate_package(package)
    expected = os.environ.get("QORQUT_REAL_EMAIL_MANAGEMENT_DIGEST")
    assert expected is None or package.digest == expected


def test_every_operation_the_real_roles_name_is_registered(host):
    """Loading validates each default-policy name against the core's registry; there is no tolerated unknown name."""
    registry = host.runtime.registry
    for role in ROLE_IDS:
        package = load_package(ROLES, f"roles/imported-roles/native/roles/{role}/role.yaml", "role", registry)
        unknown = [name for name in package.manifest["default_tool_policy"] if registry.metadata(name).name != name]
        assert unknown == [], f"{role} names operations this core does not register"
        assert not [name for name in package.manifest["default_tool_policy"] if MAIL_NAME.fullmatch(name)]
        if role == "ordubashy":
            expected = os.environ.get("QORQUT_REAL_ORDUBASHY_DIGEST")
            assert expected is None or package.digest == expected


def test_neither_real_package_mentions_a_mail_operation():
    for root in (PACKAGE, ROLES / "roles"):
        found = {(str(path.relative_to(root)), hit) for path in root.rglob("*")
                 if path.is_file() and ".git" not in path.relative_to(root).parts and path.suffix in {".md", ".yaml", ".json"}
                 and "tests" not in path.relative_to(root).parts
                 for hit in MAIL_NAME.findall(path.read_text(errors="replace"))}
        assert not found


def test_the_workspace_copy_of_the_role_is_the_unmodified_source(host):
    for role in ROLE_IDS:
        source = ROLES / "roles/imported-roles/native/roles" / role / "role.yaml"
        assert (Path(host.workspace) / "qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/imported-roles/native/roles" / role / "role.yaml").read_bytes() == source.read_bytes()
        assert publish_role(host, role)["id"] == role


def test_real_preparation_seeds_the_owner_instructions_and_resolves_the_skill(host):
    ordubashy = create_agent(host, "ordubashy", "ordubashy")
    assert grants(host, ordubashy) == [{**host.pin, "config": {}}]
    prepared = prepare_agent(host.runtime.resolver, Principal(ordubashy, "agent"), endpoint="http://127.0.0.1:9988", versions=VERSIONS)
    home = host.paths.extension_home("email-management")
    assert prepared["ready"] and prepared["projection_drift"] == []
    assert prepared["instance_guidance"] == {"email-management": {"path": str(home), "seeded": True}}
    template = PACKAGE / "skills/email-management/instance-template"
    seeded = sorted(str(item.relative_to(home)) for item in home.rglob("*") if item.is_file() and item.name != ".origin.json")
    assert seeded == sorted(str(item.relative_to(template)) for item in template.rglob("*") if item.is_file())
    for item in template.rglob("*"):
        if item.is_file():
            assert (home / item.relative_to(template)).read_bytes() == item.read_bytes()
    workspace = Path(prepared["workspace"])
    for root in (".agents/skills", ".claude/skills"):
        text = (workspace / root / SKILL / "SKILL.md").read_text()
        assert str(home) in text and PLACEHOLDER not in text
