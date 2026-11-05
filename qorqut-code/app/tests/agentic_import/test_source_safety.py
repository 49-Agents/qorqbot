"""Regression cases from Manager review; every file and helper is synthetic."""

import os
import shlex
import subprocess

import pytest

from bazarlyk.agentic import ImportFailure
from bazarlyk.agentic import support
from bazarlyk.agentic.support import Source
from conftest import commit, write


def git(root, *args):
    return subprocess.run(["/usr/bin/git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    write(root, "nested/asset.txt", "original source bytes")
    write(root, "asset.txt", "original source bytes")
    commit(root)
    return Source("custom-fixture", str(root), "owner", True, [])


def test_git_inspection_never_executes_repository_or_ambient_helpers(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    write(root, "asset.txt", "asset\n")
    write(root, ".gitattributes", "*.txt filter=tripwire\n")
    commit(root)  # No filter is configured during fixture creation.
    expected_commit = git(root, "rev-parse", "HEAD")
    markers = []
    for name in ("fsmonitor", "clean", "smudge", "process", "hook", "fake-git"):
        marker, helper = tmp_path / (name + ".marker"), tmp_path / name
        helper.write_text("#!/bin/sh\nprintf invoked > " + shlex.quote(str(marker)) + "\nexit 0\n")
        helper.chmod(0o755)
        markers.append(marker)
    for key, value in {
        "core.fsmonitor": str(tmp_path / "fsmonitor"),
        "filter.tripwire.clean": str(tmp_path / "clean"),
        "filter.tripwire.smudge": str(tmp_path / "smudge"),
        "filter.tripwire.process": str(tmp_path / "process"),
        "filter.tripwire.required": "true",
        "core.hooksPath": str(tmp_path / "hooks"),
    }.items():
        git(root, "config", "--local", key, value)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/post-index-change").symlink_to(tmp_path / "hook")
    # Force a worktree content check, which would normally invoke the clean/process filter.
    os.utime(root / "asset.txt", (1234567890, 1234567890))
    redirect = tmp_path / "redirect"
    redirect.mkdir()
    write(redirect, "different.txt", "different repository")
    commit(redirect)
    git(root, "config", "--local", "core.worktree", str(redirect))
    for key, value in {
        "GIT_DIR": str(redirect / ".git"), "GIT_WORK_TREE": str(redirect),
        "GIT_INDEX_FILE": str(redirect / ".git/index"),
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "fsmonitor"),
        "GIT_CONFIG_GLOBAL": str(tmp_path / "ambient-config"),
        "GIT_CONFIG_PARAMETERS": "'core.fsmonitor='" + shlex.quote(str(tmp_path / "fsmonitor")),
        "GIT_OBJECT_DIRECTORY": str(redirect / ".git/objects"),
        "HOME": str(tmp_path / "home"), "PATH": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "git").symlink_to(tmp_path / "fake-git")
    inspected = Source("custom-fixture", str(root), "owner", True, [])
    assert inspected.commit == expected_commit
    assert inspected.clean is True
    assert not inspected.diagnostics
    assert inspected.git("config", "--get", "core.fsmonitor").strip() == "false"
    assert inspected.git("config", "--get", "core.hooksPath").strip() == os.devnull
    assert inspected.git("config", "--get", "filter.tripwire.process").strip() == ""
    assert inspected.git("config", "--get", "filter.tripwire.required").strip() == "false"
    assert not any(marker.exists() for marker in markers)


def test_git_output_is_bounded(source, monkeypatch):
    monkeypatch.setattr(support, "MAX_GIT_OUTPUT_BYTES", 8)
    with pytest.raises(ImportFailure) as error:
        source.git("rev-parse", "HEAD")
    assert error.value.code == "source_git_output_limit"


def test_leaf_replacement_cannot_follow_a_symlink(source, tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-root")
    original_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "asset.txt" and not swapped:
            swapped = True
            target = source.root / "asset.txt"
            target.unlink()
            target.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(support.os, "open", swap_before_open)
    with pytest.raises(ImportFailure) as error:
        source.read("asset.txt")
    assert error.value.code == "unsafe_path"
    assert not source.used


def test_parent_replacement_cannot_follow_a_symlink(source, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "asset.txt", "outside-root")
    original_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "nested" and not swapped:
            swapped = True
            (source.root / "nested").rename(source.root / "saved")
            (source.root / "nested").symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(support.os, "open", swap_before_open)
    with pytest.raises(ImportFailure) as error:
        source.read("nested/asset.txt")
    assert error.value.code == "unsafe_path"
    assert not source.used


def test_open_parent_stays_confined_after_path_replacement(source, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "asset.txt", "outside-root")
    original_open = os.open
    swapped = False

    def swap_after_parent_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "asset.txt" and not swapped:
            swapped = True
            (source.root / "nested").rename(source.root / "saved")
            (source.root / "nested").symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(support.os, "open", swap_after_parent_open)
    assert source.read("nested/asset.txt") == b"original source bytes"


def test_oversized_file_is_rejected_before_read(source, monkeypatch):
    monkeypatch.setattr(support, "MAX_FILE_BYTES", 8)

    def never_read(*args):
        raise AssertionError("Oversized data must not be read")

    monkeypatch.setattr(support.os, "read", never_read)
    with pytest.raises(ImportFailure) as error:
        source.read("asset.txt")
    assert error.value.code == "oversized_resource"
    assert not source.used


def test_growing_file_cannot_exceed_read_limit(source, monkeypatch):
    (source.root / "asset.txt").write_bytes(b"1234")
    monkeypatch.setattr(support, "MAX_FILE_BYTES", 8)
    original_read = os.read
    grew = False

    def grow_then_read(descriptor, count):
        nonlocal grew
        if not grew:
            grew = True
            with (source.root / "asset.txt").open("ab") as stream:
                stream.write(b"567890123456")
        return original_read(descriptor, count)

    monkeypatch.setattr(support.os, "read", grow_then_read)
    with pytest.raises(ImportFailure) as error:
        source.read("asset.txt")
    assert error.value.code == "oversized_resource"
    assert not source.used


def test_special_file_is_rejected_without_blocking(source):
    os.mkfifo(source.root / "pipe")
    with pytest.raises(ImportFailure) as error:
        source.read("pipe")
    assert error.value.code == "unsafe_path"
