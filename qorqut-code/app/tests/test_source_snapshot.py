"""The same deterministic export contract used by all three consumers."""
import hashlib
import json
import os

import pytest

from bazarlyk.source_snapshot import canonical, component_digest, component_records, snapshot_sha256, validate_export, SnapshotError


def fixture(tmp_path):
    parent = tmp_path / "host"
    child = parent / "nested"
    child.mkdir(parents=True)
    (parent / "plain").write_bytes(b"host\n")
    (child / "plain").write_bytes(b"child\n")
    components = [dict(name=name, destination=dest, commit="a" * 40, tree="b" * 40)
                  for name, dest in (("host", "host"), ("nested", "host/nested"))]
    for c in components:
        c["snapshot_sha256"] = component_digest(tmp_path, c, components)
    manifest = dict(format=1, components=components)
    (tmp_path / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest))
    return manifest


def test_exact_records_and_independent_nested_ownership(tmp_path):
    manifest = fixture(tmp_path)
    expected = [{"path": "plain", "kind": "file", "sha256": hashlib.sha256(b"host\n").hexdigest(), "executable": False}]
    assert manifest["components"][0]["snapshot_sha256"] == hashlib.sha256(canonical(expected)).hexdigest()
    validate_export(tmp_path)
    (tmp_path / "host/nested/plain").write_text("changed")
    assert component_digest(tmp_path, manifest["components"][0], manifest["components"]) == manifest["components"][0]["snapshot_sha256"]
    with pytest.raises(SnapshotError, match="nested"):
        validate_export(tmp_path)


@pytest.mark.parametrize("mutation", ["bytes", "mode", "new", "removed", "symlink", "fifo"])
def test_snapshot_detects_actual_export_tampering(tmp_path, mutation):
    fixture(tmp_path)
    path = tmp_path / "host/plain"
    if mutation == "bytes": path.write_bytes(b"changed")
    if mutation == "mode": path.chmod(0o755)
    if mutation == "new": (path.parent / "new").write_text("new")
    if mutation == "removed": path.unlink()
    if mutation == "symlink":
        path.unlink(); path.symlink_to("missing")
    if mutation == "fifo":
        path.unlink(); os.mkfifo(path)
    with pytest.raises(SnapshotError): validate_export(tmp_path)


@pytest.mark.parametrize("mutation", ["legacy", "duplicate_name", "duplicate_destination", "traversal", "digest", "id", "nested_link"])
def test_malformed_manifest_and_ownership_rejected(tmp_path, mutation):
    value = fixture(tmp_path)
    first, second = value["components"]
    if mutation == "legacy": first.pop("snapshot_sha256")
    if mutation == "duplicate_name": second["name"] = first["name"]
    if mutation == "duplicate_destination": second["destination"] = first["destination"]
    if mutation == "traversal": first["destination"] = "../host"
    if mutation == "digest": first["snapshot_sha256"] = "x" * 64
    if mutation == "id": first["commit"] = "a"
    if mutation == "nested_link":
        (tmp_path / "host/nested/plain").unlink()
        (tmp_path / "host/nested").rmdir()
        (tmp_path / "host/nested").symlink_to(tmp_path)
    (tmp_path / "SOURCE-MANIFEST.json").write_text(json.dumps(value))
    with pytest.raises((SnapshotError, OSError)): validate_export(tmp_path)


def test_symlinks_hash_literal_target_and_do_not_follow(tmp_path):
    (tmp_path / "target").write_text("one")
    (tmp_path / "link").symlink_to("target")
    one = snapshot_sha256(tmp_path)
    (tmp_path / "target").write_text("two")
    assert snapshot_sha256(tmp_path) != one
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("missing")
    snapshot_sha256(tmp_path)


def test_component_witness_matches_format_one_records(tmp_path):
    value = fixture(tmp_path)
    component = value['components'][0]
    (tmp_path / 'host/plain').chmod(0o755)
    (tmp_path / 'host/link').symlink_to('plain')
    records = component_records(tmp_path, component, value['components'])
    assert records == [{'path': 'link', 'kind': 'symlink', 'target': 'plain'},
                       {'path': 'plain', 'kind': 'file', 'sha256': hashlib.sha256(b'host\n').hexdigest(), 'executable': True}]
    assert component_digest(tmp_path, component, value['components']) == hashlib.sha256(canonical(records)).hexdigest()
