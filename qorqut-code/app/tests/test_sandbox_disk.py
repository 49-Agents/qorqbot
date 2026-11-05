import os
from types import SimpleNamespace
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.disk import DiskBudgetAdmission, DiskReport, _GIB
from bazarlyk.sandbox.lifecycle import SandboxLifecycle
from bazarlyk.sandbox.lifecycle import EnvironmentResult
from bazarlyk.sandbox.state import SandboxStateStore, environment_status
from foundation_support import BUSINESS, database_at

OTHER = "00000000-0000-4000-8000-000000000003"


def config(disk_gb=1):
    return HostConfig("sha256:" + "a" * 64, "sha256:" + "b" * 64,
                      ResourceLimits(128, 128, 64, 1, 96, disk_gb), 1148, enabled=True)


def paths_and_database(tmp_path, *, other=False):
    database = database_at(tmp_path)
    workspace = tmp_path / "first"
    (workspace / "hq").mkdir(parents=True)
    business = {"id": BUSINESS, "workspace": str(workspace), "hq_path": str(workspace / "hq")}
    paths = BusinessPaths.registered(tmp_path, business)
    with database.transaction() as uow:
        SandboxStateStore().insert_intent(uow, BUSINESS)
        if other:
            uow.repository("business_sandboxes").insert({"business_id": OTHER,
                                                            "desired_generation": 1, "state": "pending"})
    return database, paths


def test_disk_accounting_deduplicates_hardlinks_and_charges_sparse_physical_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr("bazarlyk.sandbox.disk.os.statvfs", lambda _: SimpleNamespace(f_bavail=16 * _GIB, f_frsize=1))
    database, paths = paths_and_database(tmp_path)
    paths.workspace.joinpath("hq", "one").write_bytes(b"canary")
    paths.workspace.joinpath("hq", "same").hardlink_to(paths.workspace / "hq" / "one")
    sparse = paths.workspace / "hq" / "sparse"
    with sparse.open("wb") as stream:
        stream.seek(8 * 1024 * 1024 - 1)
        stream.write(b"x")
    admission = DiskBudgetAdmission(tmp_path, host_floor_gb=1)
    report = admission.check(database, paths, config())
    assert report.admitted
    assert report.used_bytes >= sparse.stat().st_blocks * 512
    assert report.used_bytes < sparse.stat().st_size + paths.workspace.joinpath("hq", "one").stat().st_size
    assert 0 < report.reserved_bytes < _GIB


def test_disk_accounting_counts_but_never_follows_internal_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr("bazarlyk.sandbox.disk.os.statvfs", lambda _: SimpleNamespace(f_bavail=16 * _GIB, f_frsize=1))
    database, paths = paths_and_database(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.workspace.joinpath("hq", "escape").symlink_to(outside, target_is_directory=True)
    (paths.workspace / "hq" / "venv").mkdir()
    (paths.workspace / "hq" / "venv" / "bin").mkdir()
    (paths.workspace / "hq" / "venv" / "bin" / "python").symlink_to("/usr/bin/python3")
    report = DiskBudgetAdmission(tmp_path).check(database, paths, config())
    assert report.admitted
    assert report.used_bytes < 1024 * 1024


def test_disk_accounting_uses_directory_fd_when_child_is_replaced(tmp_path, monkeypatch):
    database, paths = paths_and_database(tmp_path)
    victim = paths.workspace / "hq" / "victim"
    victim.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped = False
    real_open = os.open

    def replace_before_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if dir_fd is not None and name == "victim" and not swapped:
            swapped = True
            victim.rmdir()
            victim.symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("bazarlyk.sandbox.disk.os.open", replace_before_open)
    report = DiskBudgetAdmission(tmp_path).check(database, paths, config())
    assert swapped and report.error_code == "environment_conflict"


def test_disk_accounting_rejects_ancestor_swap_before_foreign_traversal(tmp_path, monkeypatch):
    database, paths = paths_and_database(tmp_path)
    foreign = tmp_path.parent / (tmp_path.name + "-foreign")
    foreign.mkdir()
    (foreign / "secret-canary").write_bytes(b"must-not-be-accounted")
    original = paths.workspace.with_name(paths.workspace.name + "-original")
    swapped = False
    real_open = os.open

    def replace_ancestor_before_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if dir_fd is not None and name == paths.workspace.name and not swapped:
            swapped = True
            paths.workspace.rename(original)
            paths.workspace.symlink_to(foreign, target_is_directory=True)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    # safe_io.directory() walks each component with O_NOFOLLOW. The swap is
    # injected at the component open, after the ordinary path checks.
    monkeypatch.setattr("bazarlyk.storage.safe_io.os.open", replace_ancestor_before_open)
    report = DiskBudgetAdmission(tmp_path).check(database, paths, config())
    assert swapped and report.error_code == "environment_conflict"
    assert report.used_bytes == 0


def test_disk_admission_reserves_every_committed_business_budget(tmp_path, monkeypatch):
    database, paths = paths_and_database(tmp_path, other=True)
    fake = SimpleNamespace(f_frsize=1, f_bsize=1, f_bavail=2 * _GIB)
    monkeypatch.setattr("bazarlyk.sandbox.disk.os.statvfs", lambda _: fake)
    report = DiskBudgetAdmission(tmp_path, host_floor_gb=1).check(database, paths, config())
    assert _GIB < report.reserved_bytes <= 2 * _GIB
    assert report.error_code == "environment_unavailable"


def test_disk_scan_limits_are_fail_closed(tmp_path):
    database, paths = paths_and_database(tmp_path)
    (paths.workspace / "hq" / "file").write_text("x")
    report = DiskBudgetAdmission(tmp_path, max_entries=1).check(database, paths, config())
    assert report.error_code == "environment_unavailable"


def test_lifecycle_disk_observation_is_visible_without_paths_or_credentials(tmp_path):
    database, paths = paths_and_database(tmp_path)
    store = SandboxStateStore()
    with database.transaction() as uow:
        lease = store.claim(uow, BUSINESS)
        store.record_disk(uow, lease, {
            "used_bytes": 10, "budget_bytes": _GIB, "reserved_bytes": _GIB,
            "available_bytes": 4 * _GIB, "host_floor_bytes": _GIB,
            "checked_at": "2026-09-12T12:00:00.000000Z", "error_code": "environment_unavailable"})
    with database.read() as uow:
        value = environment_status(uow, BUSINESS)
    assert value["disk"]["status"] == "environment_unavailable"
    assert set(value["disk"]) == {"used_bytes", "budget_bytes", "checked_at", "status"}


def test_lifecycle_refuses_creation_when_disk_budget_is_exhausted(tmp_path):
    database, paths = paths_and_database(tmp_path)
    calls = []

    class Admission:
        def check(self, database, paths, config):
            return DiskReport(2 * _GIB, _GIB, _GIB, 3 * _GIB, _GIB,
                              "2026-09-12T12:00:00.000000Z", "environment_unavailable")

    class Driver:
        def reconcile(self, *args, **kwargs):
            calls.append(True)
            return EnvironmentResult("must-not-start")

    lifecycle = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: config(),
                                 disk_admission=Admission())
    row = lifecycle.ensure_business_environment(BUSINESS)
    assert row["state"] == "failed" and row["error_code"] == "environment_unavailable"
    assert row["disk_error_code"] == "environment_unavailable" and not calls


def test_disk_reservation_uses_remaining_capacity_after_verified_usage(tmp_path):
    database, paths = paths_and_database(tmp_path, other=True)
    admission = DiskBudgetAdmission(tmp_path)
    with database.transaction() as uow:
        store = SandboxStateStore()
        lease = store.claim(uow, OTHER)
        store.record_disk(uow, lease, {
            "used_bytes": _GIB // 2, "budget_bytes": _GIB, "reserved_bytes": _GIB,
            "available_bytes": 4 * _GIB, "host_floor_bytes": _GIB,
            "checked_at": "2026-09-12T12:00:00.000000Z", "error_code": None})
    report = admission.check(database, paths, config())
    assert report.reserved_bytes < 2 * _GIB
