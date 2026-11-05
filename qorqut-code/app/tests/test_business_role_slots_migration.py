"""Migration 096: one active profile per business role (Onbashy and Dev) replaces one active home profile."""

import json
import sqlite3

import pytest

from bazarlyk.agents import roles
from qorqut_operations.contracts import new_id

from bazarlyk.storage import Database
from foundation_support import BUSINESS, OTHER, database_at

AT = "2026-09-26T10:00:00.000000Z"
SCHEMA_INDEX = "agent_profiles_one_active_role_per_home"


def agent(c, business, role, *, status="active", name=None):
    identity = new_id()
    name = name or (roles.role_name(role) if role in roles.BUSINESS_ROLES else role)
    profile = {"agent_id": identity, "name": name, "display_name": name.title(), "status": status,
               "role": {"id": role, "version": "1.0.0", "digest": "sha256:" + "0" * 64},
               "scope": {"kind": "business", "business_id": business}}
    c.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES(?,'agent',?,?,?)",
              (identity, name.title(), AT, AT))
    c.execute("INSERT INTO agent_profiles(agent_id,principal_id,home_business_id,namespace,name,workspace,status,profile_json,"
              "revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
              (identity, identity, business, business, name, f"/hq/{business}/agents/{name}-{identity}", status,
               json.dumps(profile), AT, AT))
    return identity


def legacy(tmp_path, monkeypatch):
    migrations = Database.migrations()
    index = next(i for i, item in enumerate(migrations) if item[0] == 96)
    with monkeypatch.context() as patch:
        patch.setattr(Database, "migrations", staticmethod(lambda: migrations[:index]))
        database = database_at(tmp_path)
        with database.transaction() as uow:
            c = uow.connection
            onbashy = agent(c, BUSINESS, roles.BUSINESS_AGENT_ROLE)
            coder = agent(c, BUSINESS, "coder", status="retired")
            # Before 096 a second active home profile is refused whatever its role.
            with pytest.raises(sqlite3.IntegrityError):
                agent(c, BUSINESS, roles.DEV_ROLE)
        with database.read() as uow:
            rows = [tuple(row) for row in uow.connection.execute("SELECT * FROM agent_profiles ORDER BY agent_id")]
    return database, rows, onbashy, coder


def test_upgrade_keeps_onbashy_only_rosters_and_opens_the_dev_slot(tmp_path, monkeypatch):
    database, rows, onbashy, coder = legacy(tmp_path, monkeypatch)
    current = Database(database.path).initialize()
    fresh = Database(tmp_path / "fresh.sqlite3").initialize()
    with current.read() as uow, fresh.read() as other:
        c = uow.connection
        assert [tuple(row) for row in c.execute("SELECT * FROM agent_profiles ORDER BY agent_id")] == rows
        indexes = lambda conn: {row[0]: row[1] for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        assert indexes(c) == indexes(other.connection)
        assert SCHEMA_INDEX in indexes(c) and "agent_profiles_one_active_home" not in indexes(c)
        # A transitional Onbashy-only business stays readable; its missing Dev is reported, not repaired.
        assert roles.require_business_agent(c, BUSINESS)["agent_id"] == onbashy
        assert roles.dev_agent(c, BUSINESS) is None
        first = next(item for item in roles.roster_violations(c) if item["business_id"] == BUSINESS)
        assert first["missing_roles"] == [roles.DEV_ROLE] and first["duplicate_roles"] == first["invalid_roles"] == []
        assert c.execute("SELECT status FROM agent_profiles WHERE agent_id=?", (coder,)).fetchone()[0] == "retired"
    with current.transaction() as uow:
        dev = agent(uow.connection, BUSINESS, roles.DEV_ROLE)
    with current.read() as uow:
        assert roles.require_dev_agent(uow.connection, BUSINESS)["agent_id"] == dev
        assert [item for item in roles.roster_violations(uow.connection) if item["business_id"] == BUSINESS] == []


@pytest.mark.parametrize("role", [roles.BUSINESS_AGENT_ROLE, roles.DEV_ROLE])
def test_each_business_role_slot_holds_one_active_profile(tmp_path, role):
    database = database_at(tmp_path)
    with database.transaction() as uow:
        agent(uow.connection, BUSINESS, role)
        # The same role in another business and inactive or retired rows never collide.
        agent(uow.connection, OTHER, role)
        agent(uow.connection, BUSINESS, role, status="inactive", name=f"{role}-kept")
        agent(uow.connection, BUSINESS, "coder", status="retired")
    with database.transaction() as uow, pytest.raises(sqlite3.IntegrityError, match="one_active_role_per_home"):
        agent(uow.connection, BUSINESS, role, name=f"{role}-second")
    with database.read() as uow:
        assert len([row for row in roles.active_home_profiles(uow.connection, BUSINESS) if roles.profile_role(row) == role]) == 1
