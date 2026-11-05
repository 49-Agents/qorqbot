"""Proxy IP eligibility: assignments plus explicit sanitized evidence; unknown is never free."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from bazarlyk.browser import config, inventory
from qorqut_operations.contracts import DomainError
from foundation_support import BUSINESS, OTHER, database_at
from test_browser_isolated_config import EXIT, isolated, propose

NOW = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
STAMP = "2026-09-29T22:21:39Z"
HEADER = "ip,ports,persona,classification,non_hn_platforms,profile_metadata_check,exit_ip_checked_from_dushman\n"
ROWS = [
    "188.220.114.215,12323,,no-known-non-HN-account,,no common non-HN login-cookie markers found,yes: matches",
    "166.88.210.144,12323,harbor,occupied/reserved,producthunt,not needed: recorded assignment,not tested",
    "46.203.145.251,12323,,no-known-non-HN-account,,profile unavailable,not tested",
    "46.203.145.252,12323,fenwick,no-known-non-HN-account,,no common non-HN login-cookie markers found,not tested",
]


def snapshot(tmp_path, rows=ROWS, extra_header=""):
    path = tmp_path / "all-ips.csv"
    path.write_text(HEADER.replace("\n", extra_header + "\n") + "\n".join(rows) + "\n")
    return inventory.snapshot_from_csv(path, generated_at=STAMP)


def status(db, value, address, **options):
    return inventory.report(db, value, [address], now=NOW, **options)[address]


def test_csv_sanitization_drops_personas_paths_and_ports(tmp_path):
    value = snapshot(tmp_path, extra_header=",hn_platforms")
    text = json.dumps(value)
    for leaked in ("mossgrove", "harbor", "quietvale", "fenwick", "12323", "hn_platforms", str(tmp_path), "csv"):
        assert leaked not in text
    entry = value["ips"]["188.220.114.215"]
    assert entry == {"classification": "no-known-non-HN-account", "checked_at": STAMP, "profile_check": "clean",
                     "non_hn_platform_count": 0, "persona_assigned": False, "exit_verified": True}
    assert value["ips"]["166.88.210.144"]["non_hn_platform_count"] == 1
    assert value["ips"]["46.203.145.252"]["persona_assigned"] is True


@pytest.mark.parametrize("persona", ["", "mossgrove"])
@pytest.mark.parametrize("hn_accounts", ["", "legacy-hn-handle"])
def test_hn_alone_is_free_but_every_persona_assignment_blocks(tmp_path, persona, hn_accounts):
    row = ROWS[0].replace(",12323,,", f",12323,{persona},") + "," + hn_accounts
    value = snapshot(tmp_path, rows=[row], extra_header=",hn_accounts")
    result = status(database_at(tmp_path), value, EXIT)
    assert result["status"] == ("occupied" if persona else "available")
    assert result["evidence"]["persona_assigned"] is bool(persona)
    assert "legacy-hn-handle" not in json.dumps(value)


def test_legacy_snapshot_without_persona_evidence_is_unknown(tmp_path):
    value = snapshot(tmp_path)
    del value["ips"][EXIT]["persona_assigned"]
    inventory.parse_snapshot(value)  # readable, but no longer sufficient for allocation
    result = status(database_at(tmp_path), value, EXIT)
    assert result["status"] == "unknown"
    assert result["evidence"]["reason"] == "persona assignment was not checked"


def test_csv_without_persona_column_does_not_infer_no_assignment(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("ip,classification,profile_metadata_check\n"
                    f"{EXIT},no-known-non-HN-account,no common non-HN login-cookie markers found\n")
    value = inventory.snapshot_from_csv(path, generated_at=STAMP)
    assert "persona_assigned" not in value["ips"][EXIT]
    assert status(database_at(tmp_path), value, EXIT)["status"] == "unknown"


def test_csv_duplicate_cannot_overwrite_a_persona_assignment(tmp_path):
    with pytest.raises(DomainError, match="duplicate IP"):
        snapshot(tmp_path, rows=[ROWS[0].replace(",12323,,", ",12323,mossgrove,"), ROWS[0]])


def test_only_fresh_clean_unassigned_evidence_is_available(tmp_path):
    db, value = database_at(tmp_path), snapshot(tmp_path)
    ok = status(db, value, EXIT)
    assert ok["status"] == "available" and ok["evidence"]["exit_verified"] is True
    assert status(db, value, "166.88.210.144")["status"] == "occupied"
    # The audit could not check the profile: inconclusive, therefore not free.
    assert status(db, value, "46.203.145.251")["status"] == "unknown"
    # No evidence at all, a missing entry or stale evidence are never eligible.
    assert status(db, None, EXIT)["status"] == "unknown"
    assert status(db, value, "104.16.0.9")["status"] == "unknown"
    late = inventory.report(db, value, [EXIT], now=NOW + timedelta(days=8))[EXIT]
    assert late["status"] == "unknown" and "stale" in late["evidence"]["reason"]
    early = inventory.report(db, value, [EXIT], now=NOW - timedelta(days=1))[EXIT]
    assert early["status"] == "unknown"


@pytest.mark.parametrize("archived", [False, True])
def test_canonical_assignments_win_over_a_clean_audit(tmp_path, archived):
    db, value = database_at(tmp_path), snapshot(tmp_path)
    propose(db, tmp_path, BUSINESS, {"enabled": False, "proxy_server": "http://188.220.114.215:12323"})
    if archived:
        with db.transaction() as uow:
            uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (STAMP, BUSINESS))
    held = status(db, value, EXIT)
    assert held["status"] == "assigned" and held["evidence"]["assigned_to"] == ["first"]
    # The holding business itself may keep asking about its own IP.
    assert status(db, value, EXIT, business_id=BUSINESS)["status"] == "available"


def test_isolated_exit_and_literal_proxy_hosts_both_reserve(tmp_path):
    db, value = database_at(tmp_path), snapshot(tmp_path)
    propose(db, tmp_path, OTHER, isolated())
    assert status(db, value, EXIT)["status"] == "assigned"


def test_snapshot_must_be_strict_private_and_explicit(tmp_path):
    good = snapshot(tmp_path)
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(good))
    path.chmod(0o600)
    assert inventory.load_snapshot(path) == good
    path.chmod(0o666)
    with pytest.raises(DomainError):
        inventory.load_snapshot(path)
    path.chmod(0o600)
    for tamper in (lambda v: v["ips"][EXIT].update(persona="mossgrove"), lambda v: v.update(host="dushman"),
                   lambda v: v.update(source="/absolute/path/to/operations/audit"), lambda v: v.update(schema=2),
                   lambda v: v["ips"].update({"proxy.example": v["ips"][EXIT]}),
                   lambda v: v["ips"][EXIT].update(checked_at="yesterday"),
                   lambda v: v["ips"][EXIT].update(exit_verified="yes"),
                   lambda v: v["ips"][EXIT].update(persona_assigned="false"),
                   lambda v: v["ips"][EXIT].update(persona_assigned=0),
                   lambda v: v["ips"][EXIT].update(persona_assigned=None)):
        bad = json.loads(json.dumps(good))
        tamper(bad)
        with pytest.raises(DomainError):
            inventory.parse_snapshot(bad)
    with pytest.raises(DomainError):
        inventory.load_snapshot(tmp_path / "missing.json")


def test_cli_reports_without_writing(tmp_path, capsys):
    root = tmp_path / "root"
    (root / ".bazarlyk").mkdir(parents=True)
    database_at(root / ".bazarlyk")
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(snapshot(tmp_path)))
    path.chmod(0o600)
    before = (root / ".bazarlyk/state.sqlite3").read_bytes()
    assert inventory.main(["--root", str(root), "--snapshot", str(path), "166.88.210.144"]) == 0
    assert json.loads(capsys.readouterr().out)["166.88.210.144"]["status"] == "occupied"
    assert (root / ".bazarlyk/state.sqlite3").read_bytes() == before
    assert inventory.main(["--root", str(root), "--snapshot", str(tmp_path / "missing.json"), EXIT]) == 1
    # Without a snapshot the same question is answered as unknown, never as free.
    assert inventory.main(["--root", str(root), EXIT]) == 0
    assert json.loads(capsys.readouterr().out)[EXIT]["status"] == "unknown"


def test_staged_example_snapshot_is_valid_and_only_evidence(tmp_path):
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "tools/browser/isolated/browser-proxy-inventory.json.example"
    value = json.loads(example.read_text())
    inventory.parse_snapshot(value)
    assert list(value["ips"]) == [EXIT] and set(value) == {"schema", "generated_at", "source", "ips"}
    db = database_at(tmp_path)
    # The pilot's recorded persona is not evidence of a free IP for another business.
    assert status(db, value, EXIT)["status"] == "occupied"
