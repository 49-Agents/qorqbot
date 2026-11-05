"""A synthetic host for the relocated tables: real engine parts, real Git, fake projects and agents.

The operational test host with the table declarations spliced in, the table record hook and Global rule installed where a
trusted host installs them, and the real program registry/background service running the table history publisher against
a throwaway repository. No product code, provider, live data or network is involved."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest import mock

import operational_support
from operational_support import BUSINESS, COORDINATOR, OTHER, OperationalHost
from qorqut_operations import background
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, schema
from qorqut_operations.background import BackgroundAuthority, BackgroundService, Program, ProgramRegistry
from qorqut_operations.contracts import InvocationContext, Principal
from qorqut_operations.policy import PolicyRules
from qorqut_operations.policy.resolver import AgentPolicyBasis
from qorqut_operations.storage import StorageContribution
from qorqut_operations.tables import (GLOBAL_TABLE_OPERATIONS, IMMUTABLE_FIELDS, SCHEMA, TABLES, TableOperations,
                                      force_table_revision, global_tables)
from relationship_support import install

ELIGIBLE = "00000000-0000-4000-8000-0000000000e1"   # an active ecosystem agent holding read_global_context, not Ordubashy
COLUMNS = [{"id": "name", "name": "Name", "type": "text"}, {"id": "amount", "name": "Amount", "type": "number"},
           {"id": "details", "name": "Details", "type": "json"}]
# The table operations this host registers; a richer host (the CRM one) adds the membership and discussion operations itself.
TABLE_OPERATIONS = sorted(name for name in GLOBAL_TABLE_OPERATIONS if name.startswith("tables_") and not name.endswith(("_thread_get", "_comment", "_follow")))


def unused(*args):
    raise AssertionError("Only the table operations are exercised here")


class TablesHost(OperationalHost):
    """Tasks, Signoffs and the engine, plus the tables: declarations spliced in, hooks and policy installed, jobs runnable."""

    def __init__(self, tmp_path, *, extra=()):
        self.root = tmp_path / "operations"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.hq_calls = []
        with mock.patch.object(operational_support, "install", self._compose):
            super().__init__(self.root, extra=(background.storage_contribution(),
                                               StorageContribution("tables", TABLES, IMMUTABLE_FIELDS, SCHEMA), *extra))
        self.policy.rules = PolicyRules(before_override=(global_tables,))
        self.policy.worker_profiles = self.authority.worker_profiles
        self.policy.job_consequence_verifier = self.authority.verify_consequence
        self.policy.job_plan_validator = self.authority.validate_plan
        self.database.demo.hooks.force_revision = force_table_revision
        self.background = BackgroundService(self.app, self.programs, self.authority, locks=self.locks, clock=self.clock)
        with self.database.transaction() as uow:
            self.authority.register_workers(uow, [BUSINESS, OTHER])
        self.roles, self.grants = {COORDINATOR: "ordubashy"}, {}
        self.agents.defaults.update({name: "allow" for name in TABLE_OPERATIONS})
        self.agents.policy_basis = lambda ctx, uow: AgentPolicyBasis(
            self.agents.defaults, self.grants.get(ctx.principal_id, {}), {"id": self.roles.get(ctx.principal_id, "global-worker"), "version": "1"}, 1)
        with self.database.transaction() as uow:
            uow.repository("principals").insert({"id": ELIGIBLE, "kind": "agent", "display_name": "Eligible"})
            uow.connection.execute(
                "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, profile_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'now', 'now')",
                (ELIGIBLE, ELIGIBLE, None, "ecosystem", "agent-e1", "/nonexistent/" + ELIGIBLE, json.dumps({
                    "status": "active", "role": {"id": "global-worker"}, "scope": {
                        "kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}})))

    def _compose(self, registry, *contributions):
        self.programs = ProgramRegistry()
        self.tables = self.build_tables()
        arguments, run = schema({"version_id": UUID}, ("version_id",)), self.tables.history.run
        self.programs.register(Program("tables.version", 1, arguments, run, external=True, recover=run))
        self.programs.register(Program("tables.global-version", 1, arguments, run, external=True, recover=run,
                                       scope_kinds=("global",), global_capabilities=("read_global_context",)))
        self.authority = BackgroundAuthority(self.database, self.programs)
        self.programs.freeze()
        self.tables.register(registry)
        self.extend(registry)
        # The edge a new table creates is judged as ``relationships_create``; this host only needs that operation known.
        registry.register(OperationDescriptor("relationships_create", {"type": "object"}, prepare=unused, apply=unused))
        return install(registry, *contributions)

    def extend(self, registry):
        """Where a richer test host registers its own operations before the registry is frozen."""

    def build_tables(self):
        return TableOperations(self.database, self.root, self.project_hq, clock=self.clock, job_programs=self.programs)

    def project_hq(self, business):
        self.hq_calls.append(business["id"])
        return Path(business["hq_path"])

    # -- calls ------------------------------------------------------------------------------------------------------

    def eligible(self):
        return InvocationContext(Principal(ELIGIBLE, "agent"), allowed_business_ids=frozenset({BUSINESS, OTHER}),
                                 global_capabilities=frozenset({"read_global_context"}), profile_revision=1, source="mcp")

    def make(self, title="CRM", ctx=None, **extra):
        return self.data("tables_create", {"title": title, "columns": COLUMNS, **extra}, ctx)

    def write(self, table, rows=(), delete=(), revision=None, ctx=None, **extra):
        arguments = {"table_id": table["id"], "rows": list(rows), **extra}
        if delete:
            arguments["delete_ids"] = list(delete)
        return self.data("tables_rows_write", arguments, ctx, expected_revision=revision)

    def run(self, job_id):
        result = self.background.run_one(job_id)
        assert result["state"] == "succeeded", result
        return result

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def versions(self, business=BUSINESS):
        return self.query("SELECT * FROM table_versions WHERE business_id IS ? ORDER BY sequence", business)
