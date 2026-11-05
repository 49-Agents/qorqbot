import hashlib
import os

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.storage import safe_io
from bazarlyk.storage.files import RegisteredFileTarget


def test_parent_swap_during_atomic_replacement_cannot_write_foreign_files(tmp_path, monkeypatch):
    own, foreign, moved = tmp_path / "own", tmp_path / "foreign", tmp_path / "moved"
    own.mkdir()
    foreign.mkdir()
    target = own / "credential"
    target.write_bytes(b"before")
    (foreign / "credential").write_bytes(b"foreign secret")
    replace = os.replace

    def attack(source, destination, **kwargs):
        own.rename(moved)
        own.symlink_to(foreign, target_is_directory=True)
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(safe_io.os, "replace", attack)
    safe_io.atomic_write(target, b"own replacement", expected_hash=hashlib.sha256(b"before").hexdigest())
    assert (foreign / "credential").read_bytes() == b"foreign secret"
    assert (moved / "credential").read_bytes() == b"own replacement"
    assert sorted(path.name for path in moved.iterdir()) == ["credential"]
    with pytest.raises(DomainError):
        safe_io.read_bytes(target)


def test_parent_swap_during_delete_cannot_delete_foreign_files(tmp_path, monkeypatch):
    own, foreign, moved = tmp_path / "own", tmp_path / "foreign", tmp_path / "moved"
    own.mkdir()
    foreign.mkdir()
    (own / "file").write_bytes(b"before")
    (foreign / "file").write_bytes(b"foreign")
    unlink = os.unlink

    def attack(name, **kwargs):
        own.rename(moved)
        own.symlink_to(foreign, target_is_directory=True)
        return unlink(name, **kwargs)

    monkeypatch.setattr(safe_io.os, "unlink", attack)
    safe_io.unlink(own / "file", expected_hash=hashlib.sha256(b"before").hexdigest())
    assert (foreign / "file").read_bytes() == b"foreign"
    assert not (moved / "file").exists()


def test_read_does_not_follow_a_final_symlink_or_block_on_fifo(tmp_path):
    (tmp_path / "outside").write_bytes(b"private")
    (tmp_path / "link").symlink_to(tmp_path / "outside")
    os.mkfifo(tmp_path / "fifo")
    for name in ("link", "fifo"):
        with pytest.raises(DomainError):
            safe_io.read_bytes(tmp_path / name)


def test_registered_root_itself_cannot_be_a_symlink(tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "hq").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(DomainError):
        RegisteredFileTarget(tmp_path / "hq", "file").resolve()


def test_mode_repair_and_expected_hash_remain_bound_to_file(tmp_path):
    path = tmp_path / "new/nested/credential"
    assert safe_io.atomic_write(path, b"secret")
    path.chmod(0o644)
    assert safe_io.atomic_write(path, b"secret", enforce_mode=True)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not safe_io.atomic_write(path, b"secret", enforce_mode=True)
    with pytest.raises(DomainError, match="changed before"):
        safe_io.atomic_write(path, b"changed", expected_hash=None)
    assert safe_io.read_bytes(path) == b"secret"
