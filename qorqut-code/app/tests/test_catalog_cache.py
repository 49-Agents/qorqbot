"""Validated packages are reused only while the published store is byte-identical."""

import os

import pytest

import bazarlyk.catalog.packages as packages
from qorqut_operations.contracts import DomainError
from test_agent_configuration import Fixture


@pytest.fixture
def counted(monkeypatch):
    loads = []
    original = packages.load_package

    def counting(source, manifest_path, kind, registry):
        loads.append(str(source))
        return original(source, manifest_path, kind, registry)

    monkeypatch.setattr(packages, "load_package", counting)
    return loads


def test_catalog_reuses_validated_package_until_store_bytes_change(tmp_path, counted):
    fixture = Fixture(tmp_path)
    catalog, ref = fixture.catalog, fixture.refs["global-marketing"]
    first = catalog.get("role", ref)
    assert catalog.get("role", ref) is first and counted == [str(catalog.artifact_path(first))]

    root = catalog.artifact_path(first)
    # Select a validatable file: keep the tamper marker within the
    # 10,000-character brief limit to avoid changing the failure from a
    # published-byte conflict into an invalid-artifact size rejection.
    name = next(name for name in sorted(first.files)
                if len(first.files[name]) + len(b"\n# tampered\n") <= 10_000)
    target = root / name
    stat = target.stat()
    # An identical rewrite changes the inventory but not the bytes: re-hashed, still reused.
    target.write_bytes(first.files[name])
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert catalog.get("role", ref) is first and len(counted) == 1

    target.write_bytes(first.files[name] + b"\n# tampered\n")
    with pytest.raises(DomainError) as changed:
        catalog.get("role", ref)
    assert changed.value.code == "artifact_conflict" and "bytes changed" in changed.value.message
    assert len(counted) == 2

    target.write_bytes(first.files[name])
    extra = root / "unexpected.txt"
    extra.write_text("not published")
    with pytest.raises(DomainError) as added:
        catalog.get("role", ref)
    assert added.value.code == "artifact_conflict" and "file set changed" in added.value.message

    extra.unlink()
    restored = catalog.get("role", ref)
    assert restored.digest == first.digest and restored.manifest == first.manifest
    assert catalog.get("role", ref) is restored

    link = root / "linked.md"
    link.symlink_to(target)
    with pytest.raises(DomainError) as linked:
        catalog.get("role", ref)
    assert linked.value.code == "artifact_conflict"


def test_catalog_keys_cache_by_exact_reference_and_store(tmp_path, counted):
    fixture = Fixture(tmp_path)
    catalog = fixture.catalog
    marketing = catalog.get("role", fixture.refs["global-marketing"])
    business = catalog.get("role", fixture.refs["business-agent"])
    assert marketing is not business and len(counted) == 2
    wrong = {**fixture.refs["global-marketing"], "digest": fixture.refs["business-agent"]["digest"]}
    with pytest.raises(DomainError) as missing:
        catalog.get("role", wrong)
    assert missing.value.code == "missing_artifact" and len(counted) == 2
    plugin = catalog.get("plugin", fixture.refs["email-management"])
    tools = packages.declared_plugin_tools(plugin)
    assert tools and packages.declared_plugin_tools(plugin) is tools
