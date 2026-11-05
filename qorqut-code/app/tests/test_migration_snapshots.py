import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from bazarlyk.migration.authority import AuthorityError
from bazarlyk.migration.snapshots import (
    SourceManifest, inventory_sources, restore_snapshot, snapshot_sources,
    verify_snapshot, verify_sources,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "businesses"
    hq = root / "shop/hq"
    (hq / "tasks").mkdir(parents=True)
    (hq / "business.yaml").write_text("id: shop\nname: A Shop\nowner: user\n")
    (hq / "tasks/tasks.jsonl").write_text('{"id":"task-one"}\n')
    (hq / ".env").write_text("PROVIDER_TEST_SECRET=not-a-real-key\n")
    (hq / ".env").chmod(0o600)
    (root / "context").mkdir()
    (root / "context/ecosystem.md").write_text("Shared authored context\n")
    (root / "CLAUDE.md").write_text("Original root instructions\n")
    return root


def test_inventory_is_stable_and_excludes_code_and_machine_cache(source):
    first = inventory_sources(source)
    for directory in (source / "shop/code", source / "shop/hq/.git", source / ".bazarlyk"):
        directory.mkdir(parents=True)
        (directory / "ignored").write_text("not operational input")
    # This is a cache exclusion fixture, not an invalid repository fixture.
    (source / "shop/hq/.git/ignored").unlink()
    (source / "shop/hq/.git").rmdir()
    second = inventory_sources(source)
    assert first.fingerprint == second.fingerprint
    assert first.to_dict()["businesses"] == [{"slug": "shop", "hq": "shop/hq"}]
    names = {item["path"] for item in second.data["files"]}
    assert names == {"shop/hq/business.yaml", "shop/hq/tasks/tasks.jsonl", "shop/hq/.env",
                     "context/ecosystem.md", "CLAUDE.md"}
    assert "not-a-real-key" not in json.dumps(second.to_dict())


def test_snapshot_and_empty_directory_restore_preserve_exact_bytes(source, tmp_path):
    manifest = inventory_sources(source)
    backup = source / ".bazarlyk/migrations/rehearsal"
    result = snapshot_sources(manifest, backup)
    assert result["source_fingerprint"] == manifest.fingerprint
    assert verify_snapshot(backup).to_dict() == manifest.to_dict()
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "sources.tar").stat().st_mode & 0o777 == 0o600
    target = tmp_path / "restored"
    restored = restore_snapshot(backup, target)
    assert restored.fingerprint == manifest.fingerprint
    for entry in manifest.data["files"]:
        assert (target / entry["path"]).read_bytes() == (source / entry["path"]).read_bytes()
        assert (target / entry["path"]).stat().st_mode & 0o777 == entry["mode"]
    with pytest.raises(AuthorityError, match="empty"):
        restore_snapshot(backup, target)
    with pytest.raises(AuthorityError, match="immutable"):
        snapshot_sources(manifest, backup)


@pytest.mark.parametrize("change", ["modify", "add", "delete", "chmod"])
def test_source_changes_require_new_rehearsal(source, tmp_path, change):
    manifest = inventory_sources(source)
    file = source / "shop/hq/tasks/tasks.jsonl"
    if change == "modify":
        file.write_text("changed")
    elif change == "add":
        (file.parent / "new.jsonl").write_text("new")
    elif change == "delete":
        file.unlink()
    else:
        file.chmod(0o600)
    with pytest.raises(AuthorityError) as caught:
        verify_sources(manifest)
    assert caught.value.code == "source_changed"
    destination = tmp_path / "backup"
    with pytest.raises(AuthorityError):
        snapshot_sources(manifest, destination)
    assert not destination.exists()


def test_source_symlinks_require_explicit_disposition(source, tmp_path):
    external = tmp_path / "external"
    external.write_text("not in the business tree")
    (source / "shop/hq/context-link").symlink_to(external)
    with pytest.raises(AuthorityError) as caught:
        inventory_sources(source)
    assert caught.value.code == "source_symlink"


def test_manifest_data_is_immutable(source):
    manifest = inventory_sources(source)
    data = manifest.to_dict()
    data["files"][0]["sha256"] = "0" * 64
    assert SourceManifest(data).fingerprint != manifest.fingerprint
    with pytest.raises(TypeError):
        manifest.data["files"][0]["size"] = 99


@pytest.mark.parametrize("change", ["body", "missing", "extra", "duplicate", "symlink", "traversal"])
def test_archive_tampering_is_detected_before_restore(source, tmp_path, change):
    manifest = inventory_sources(source)
    backup = tmp_path / "backup"
    snapshot_sources(manifest, backup)
    entries = list(manifest.data["files"])
    with tarfile.open(backup / "sources.tar", "w:") as archive:
        for index, entry in enumerate(entries):
            if change == "missing" and index == 0:
                continue
            info = tarfile.TarInfo(entry["path"])
            body = (source / entry["path"]).read_bytes()
            if change == "body" and index == 0:
                body = b"x" * len(body)
            if change == "traversal" and index == 0:
                info.name = "../outside"
            info.mode, info.size = entry["mode"], len(body)
            if change == "symlink" and index == 0:
                info.type, info.linkname, info.size = tarfile.SYMTYPE, "/etc/passwd", 0
            archive.addfile(info, io.BytesIO(body))
            if change == "duplicate" and index == 0:
                archive.addfile(info, io.BytesIO(body))
        if change == "extra":
            archive.addfile(tarfile.TarInfo("extra"), io.BytesIO(b""))
    target = tmp_path / "restore"
    with pytest.raises(AuthorityError):
        restore_snapshot(backup, target)
    assert not target.exists()


@pytest.mark.parametrize("value", [[], {}, {"schema_version": True}, {"schema_version": 9}])
def test_malformed_manifest_is_rejected(value):
    with pytest.raises(AuthorityError):
        SourceManifest(value)


def test_isolated_browser_profile_is_private_runtime_state_not_migration_input(source):
    profile = source / "shop/hq/browser/profile"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("private persona state")
    (profile / "SingletonLock").symlink_to("host-1234")  # would otherwise be a refused symbolic link
    os.mkfifo(profile / "SingletonSocket")  # and a special file
    names = {item["path"] for item in inventory_sources(source).data["files"]}
    assert names == {"shop/hq/business.yaml", "shop/hq/tasks/tasks.jsonl", "shop/hq/.env", "context/ecosystem.md", "CLAUDE.md"}
    # Only the top-level browser folder is private; an authored folder that happens to share the name is not.
    (source / "shop/hq/tasks/browser").mkdir()
    (source / "shop/hq/tasks/browser/notes.md").write_text("authored")
    assert "shop/hq/tasks/browser/notes.md" in {item["path"] for item in inventory_sources(source).data["files"]}
