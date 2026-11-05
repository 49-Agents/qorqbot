"""The mandatory baseline-created pending-work cases S1-S4 of the module-split plan (section 11.1).

Every case has the same five parts, so a candidate continuation is judged exactly as the baseline control is:

* ``capture`` (baseline code only): create the pending work with the real stock-host fixtures and operations, then stop at the
  controlled interruption point. Nothing is written to a table or file by hand.
* ``preconditions``: what the closed checkpoint must contain before anyone continues it (re-checked at every resume).
* ``prepare``: the optional variant applied before continuing, always through the real operation or recorded as one explicit,
  named mutation (``revoked``: the saved authority is withdrawn; ``conflict``: the managed file is changed outside Qorqut).
* ``proceed``: only the real worker/recovery entry points of ``resumption_host.Opened``.
* ``expectations``: invariants of the outcome (identities kept, effects exactly once, nothing happens twice).

``gather`` reads the persisted state with plain read-only SQLite and Git, so the same facts are collected from the baseline
checkpoint and from whatever a candidate leaves behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import resumption_host as hosts
from resumption_support import (CONTEXT_REPOSITORY, OPERATIONS_ROOT, TABLE_HISTORY_REF, digest, file_state, git, git_commit_content,
                                git_commits, git_log_parents, git_refs, jsonable, open_database, sha256_bytes)

# Immutable columns of saved work, per table: a continuation may add rows and move state forward, but it must never rewrite
# these (the same ids, payload hashes, operation versions, authority bindings, principals and targets).
IDENTITY = {
    "signoffs": ("review_business_id", "review_scope_kind", "proposer_principal_id", "proposer_agent_id",
                 "initiated_by_principal_id", "operation_name", "operation_version", "proposal_revision", "payload_hash",
                 "change_set", "preview_json", "expected_targets_json", "policy_binding_json", "affected_scope_json",
                 "category", "request_key"),
    "apply_receipts": ("principal_id", "target_namespace", "operation_name", "payload_hash", "change_set", "signoff_id"),
    "jobs": ("business_id", "target_namespace", "kind", "principal_id", "initiated_by_principal_id", "arguments_json",
             "definition_ref_json", "dedupe_key"),
    "table_versions": ("business_id", "table_id", "table_revision", "sequence", "actor_id", "operation", "path", "snapshot_hash"),
    "data_tables": ("business_id", "created_by"),
    "documents": ("business_id", "namespace", "owning_agent_id", "kind", "path"),
    "context_inputs": ("business_id", "input_kind", "item_id", "revision", "operation", "payload_json", "submitted_by"),
    "context_filings": ("business_id", "source_version_id", "request_id", "job_id", "program_id", "program_version",
                        "definition_digest", "worker_id", "initiated_by", "input_id"),
    "context_source_versions": ("business_id", "source_kind", "source_id", "source_revision", "operation", "source_path",
                                "source_digest", "created_by"),
    "context_completions": ("business_id", "filing_id", "job_id", "input_id", "source_version_id"),
    "principals": ("kind", "display_name"),
}


def error_code(error):
    return error.get("code") if isinstance(error, dict) else None


# What may legitimately move: the state projection of each row (no timestamps, no run-specific identifiers).
STATE = {
    "signoffs": lambda r: {"state": r["state"], "status_revision": r["status_revision"], "error": error_code(r["error_json"]),
                           "applied": r["applied_at"] is not None, "has_result": r["apply_result_json"] is not None},
    "apply_receipts": lambda r: {"state": r["state"], "revision": r["revision"], "error": error_code(r["error_json"]),
                                 "progress": sorted((r["progress_json"] or {}).keys()), "has_result": r["result_json"] is not None},
    "jobs": lambda r: {"state": r["state"], "attempts": r["attempts"], "claimed": r["claimed_by"] is not None,
                       "error": error_code(r["error_json"]), "kind": r["kind"]},
    "table_versions": lambda r: {"sequence": r["sequence"], "git_commit": r["git_commit"], "snapshot_retained": r["snapshot_json"] is not None},
    "data_tables": lambda r: {"archived": r["archived"], "revision": r["revision"]},
    "documents": lambda r: {"revision": r["revision"], "availability": r["availability"], "content_hash": r["content_hash"],
                            "archived": r["archived"], "status": (r["metadata_json"] or {}).get("status")},
    "context_inputs": lambda r: {"state": r["state"], "attempt": r["attempt"], "published": r["context_event_id"] is not None,
                                 "error": error_code(r["error_json"])},
    "context_filings": lambda r: {"input": r["input_id"] is not None},
    "context_source_versions": lambda r: {},
    "context_completions": lambda r: {"state": r["state"], "outcome": r["outcome"], "published": r["published_commit"] is not None},
    "principals": lambda r: {"active": r["active"]},
}
COUNTED = ("signoffs", "apply_receipts", "resource_claims", "jobs", "table_versions", "data_tables", "data_rows", "documents",
           "qbdv_document_versions", "context_inputs", "context_input_attempts", "context_filings", "context_completions",
           "context_source_versions")


def git_facts(fixture: Path) -> dict:
    """Git state of the two repositories a continuation may publish to, read with ordinary read-only Git.

    ``table_content`` is the ordered (newest first) content manifest of every table-history commit: its parents and, for every
    path in its tree, the SHA-256 of the blob bytes (and the canonical digest of the blob as JSON). ``log``/``head``/``tree`` of the
    Context graph are exact object names."""
    operations, context = Path(fixture) / "instance" / OPERATIONS_ROOT, Path(fixture) / "instance" / CONTEXT_REPOSITORY
    history = git_commits(operations, TABLE_HISTORY_REF)
    parents = git_log_parents(operations, TABLE_HISTORY_REF)
    content = [{"commit": oid, "subject": subject, "parents": parents.get(oid, []),
                "files": git_commit_content(operations, oid)} for oid, subject in history]
    log = git_commits(context, "HEAD")
    return {"operations": {"refs": git_refs(operations), "table_history": history, "table_content": content},
            "context": {"refs": git_refs(context), "commits": len(log), "log": log,
                        "head": git(context, "rev-parse", "HEAD") if log else None,
                        "tree": git(context, "rev-parse", "HEAD^{tree}") if log else None}}


def content_manifest(items) -> list:
    """The table-history content manifest without object names: subject, parent position and file digests, newest first.
    Content digests are kept exactly; only the commit names (which carry no content) are left out."""
    position = {item["commit"]: index for index, item in enumerate(items)}
    return [{"subject": item["subject"], "parents": [position.get(parent) for parent in item["parents"]], "files": item["files"]}
            for item in items]


def context_bindings(db, rows) -> dict:
    """How the Context queue, its filings and its completions are bound, exactly as saved (event ids, jobs, source versions)."""
    attempts = db.rows("SELECT input_id, attempt, context_event_id FROM context_input_attempts ORDER BY input_id, attempt") \
        if "context_input_attempts" in db.tables() else []
    return {"inputs": {row["id"]: {"event_id": row["context_event_id"]} for row in rows.get("context_inputs", [])},
            "attempts": attempts,
            "filings": {row["id"]: {"input_id": row["input_id"], "job_id": row["job_id"], "source_version_id": row["source_version_id"]}
                        for row in rows.get("context_filings", [])},
            "completions": sorted(({"input_id": row["input_id"], "filing_id": row["filing_id"], "job_id": row["job_id"],
                                    "source_version_id": row["source_version_id"], "context_event_id": row["context_event_id"],
                                    "published_commit": row["published_commit"], "outcome": row["outcome"], "state": row["state"]}
                                   for row in rows.get("context_completions", [])), key=lambda item: item["input_id"])}


def gather(fixture: Path, *, live=False) -> dict:
    """Everything the checks and the comparison look at: identities, state, counts and Git, plus the case's managed files."""
    from resumption_support import row_fingerprints

    fixture = Path(fixture)
    with open_database(fixture, closed=not live) as db:
        present = db.tables()
        identities, state, counts, rows = {}, {}, {}, {}
        for table, columns in IDENTITY.items():
            if table not in present:
                continue
            found = db.rows(f"SELECT * FROM {table}")
            rows[table] = found
            identities[table] = row_fingerprints(found, columns)
            state[table] = {str(row["id"]): STATE[table](row) for row in found}
        counts = {table: db.count(table) for table in COUNTED if table in present}
        businesses = {row["id"]: row["hq_path"] for row in db.rows("SELECT id, hq_path FROM businesses")}
        documents = {row["id"]: row for row in rows.get("documents", [])}
        files = {}
        for identifier, row in documents.items():
            if row["kind"] == "business_owned_note" and row["business_id"] in businesses:
                files[identifier] = file_state(Path(businesses[row["business_id"]]) / row["path"])
        known = {row["id"] for row in db.rows("SELECT id FROM businesses")} | {row["agent_id"] for row in db.rows("SELECT agent_id FROM agent_profiles")}
        snapshots = {row["id"]: {"path": row["path"], "sequence": row["sequence"], "digest": digest(row["snapshot_json"])}
                     for row in rows.get("table_versions", []) if row["snapshot_json"] is not None}
        facts = {"identities": identities, "state": state, "counts": counts, "files": files, "git": git_facts(fixture),
                 "table_snapshots": snapshots, "context": context_bindings(db, rows),
                 "ids": sorted(known | {key for ids in identities.values() for key in ids})}
        facts["digest"] = digest({key: facts[key] for key in ("identities", "state", "counts", "files", "table_snapshots", "context")})
    return facts


class Checks(list):
    """Named assertions with their evidence; ``failed`` lists the ones that do not hold."""

    def __call__(self, name, condition, **detail):
        self.append({"name": name, "ok": bool(condition), "detail": jsonable(detail)})
        return bool(condition)

    @property
    def failed(self):
        return [item["name"] for item in self if not item["ok"]]


def rows_of(facts, table, **where):
    """``[(id, projection)]`` of a table's state, filtered by projection fields."""
    return [(key, item) for key, item in sorted(facts["state"].get(table, {}).items())
            if all(item.get(name) == value for name, value in where.items())]


def one(facts, table, **where):
    found = rows_of(facts, table, **where)
    return found[0] if len(found) == 1 else (None, None)


def table_commits(facts) -> list:
    return facts["git"]["operations"]["table_history"]


def added(before, after, table) -> int:
    return after["counts"].get(table, 0) - before["counts"].get(table, 0)


def common_checks(checks, before, after) -> None:
    """Invariants of every continuation: the saved identities are exactly the ones captured, nothing saved disappeared."""
    for table, ids in before["identities"].items():
        later = after["identities"].get(table, {})
        changed = sorted(key for key, value in ids.items() if key in later and later[key] != value)
        missing = sorted(key for key in ids if key not in later)
        checks(f"identities:{table}:unchanged", not changed, changed=changed)
        checks(f"identities:{table}:none-removed", not missing, missing=missing)


# -- helpers for the variants ----------------------------------------------------------------------------------------------------

def deny_operation(opened, agent_id, operation) -> dict:
    """Withdraw one saved permission of an agent through the real owner operation ``permissions_set``."""
    with opened.database.read() as uow:
        row = uow.connection.execute("SELECT revision, profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
        business = uow.connection.execute("SELECT min(id) FROM businesses WHERE archived_at IS NULL").fetchone()[0]
    overrides = {**json.loads(row["profile_json"])["tool_policy"]["overrides"], operation: "deny"}
    result = hosts.invoke(opened, hosts.owner_context(opened, business), "permissions_set",
                          {"agent_id": agent_id, "overrides": overrides}, key=f"resumption-deny-{operation}",
                          expected_revision=row["revision"])
    if result.status != "ok":
        raise RuntimeError(f"permissions_set did not apply: {result.status} {result.error}")
    return {"how": "permissions_set", "agent_id": agent_id, "operation": operation, "mode": "deny"}


def deactivate_agent(opened, agent_id) -> dict:
    """Retire an agent through the real owner operation ``agents_deactivate``."""
    with opened.database.read() as uow:
        revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()[0]
    result = hosts.invoke(opened, hosts.owner_context(opened), "agents_deactivate", {"agent_id": agent_id},
                          key="resumption-deactivate", expected_revision=revision)
    if result.status != "ok":
        raise RuntimeError(f"agents_deactivate did not apply: {result.status} {result.error}")
    return {"how": "agents_deactivate", "agent_id": agent_id}


def deactivate_worker(opened, kind) -> dict:
    """Disable the registered system worker of one job kind. No operation retires a system worker, so this is the one explicit
    mutation the baseline's own acceptance tests use (``principals.active = 0``); it is recorded as such in the evidence."""
    with opened.database.transaction() as uow:
        ids = [row[0] for row in uow.connection.execute("SELECT DISTINCT principal_id FROM jobs WHERE kind=?", (kind,))]
        for identifier in ids:
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (identifier,))
    return {"how": "principals.active=0 (no operation retires a system worker)", "kind": kind, "principals": ids}


def agent_by_name(fixture: Path, name="ordubashy") -> str:
    with open_database(fixture) as db:
        return db.one("SELECT agent_id FROM agent_profiles WHERE name=?", name)["agent_id"]


def drain_background(opened, steps, *, limit=8, label="background") -> None:
    """The background worker loop body until nothing is due."""
    for index in range(limit):
        result = opened.background_tick()
        steps.append({"step": f"{label}:{index + 1}", "result": jsonable(result)})
        if result is None:
            return


def interrupted(function, *arguments):
    """Run ``function`` expecting the controlled interruption (the process ``disappears`` there)."""
    try:
        function(*arguments)
    except hosts.ProcessStopped:
        return True
    raise AssertionError("the controlled interruption point was not reached")


# -- the cases -----------------------------------------------------------------------------------------------------------------

class Case:
    id = ""
    title = ""
    variants = ("none",)
    # Saved fields that legitimately differ between two runs of the same continuation (so they are left out of the comparison
    # only, never out of the identity or idempotence checks): table -> state fields.
    volatile = {}

    def capture(self, host, fixture: Path) -> dict:
        raise NotImplementedError

    def preconditions(self, facts, fixture: Path) -> Checks:
        raise NotImplementedError

    def prepare(self, opened, variant: str, facts: dict) -> dict:
        return {"variant": variant, "applied": None}

    def proceed(self, opened, steps: list, *, repeat=False) -> None:
        raise NotImplementedError

    def exact(self, before, after) -> dict:
        """Outcome values compared EXACTLY between baseline and candidate (no scrubbing): published content and the bindings of
        work that was already published when the checkpoint was made."""
        return {}

    def expectations(self, variant, before, after, effects, steps) -> Checks:
        raise NotImplementedError


class S1(Case):
    id = "S1"
    title = "Approved Signoff with a prepared apply receipt (global table archive)"
    variants = ("none", "revoked")

    def capture(self, host, fixture):
        from qorqut_operations.contracts import plain

        agent = host.agent(None)
        created = host.call(agent, "tables_create", {"scope": "global", "title": "Reference list",
                                                      "columns": [{"id": "name", "name": "Name", "type": "text"}]})
        assert created.status == "ok", created.error
        table = plain(created.data)["table"]
        proposal = host.call(agent, "tables_archive", {"scope": "global", "table_id": table["id"]},
                             expected_revision=table["revision"])
        assert proposal.status == "signoff_required", (proposal.status, proposal.error)
        signoff = proposal.signoff["id"]
        row = host.query("SELECT proposal_revision, payload_hash, status_revision FROM signoffs WHERE id=?", signoff)[0]
        reviewed = host.runtime.signoffs.review(host.human(None), signoff, decision="approve",
            proposal_revision=row["proposal_revision"], payload_hash=row["payload_hash"],
            expected_status_revision=row["status_revision"], idempotency_key="resumption-S1-approve")
        return {"signoff_id": signoff, "table_id": table["id"], "proposer": host.agent_id(),
                "review": jsonable(getattr(reviewed, "status", reviewed))}

    def preconditions(self, facts, fixture):
        checks = Checks()
        signoff, state = one(facts, "signoffs")
        receipt = facts["state"]["apply_receipts"].get(signoff)
        checks("one approved signoff", signoff is not None and state["state"] == "approved", signoff=signoff, state=state)
        checks("its apply receipt is prepared and bound to it", receipt is not None and receipt["state"] == "prepared",
               receipt=receipt)
        checks("the moved feature has not been applied", all(item["archived"] == 0 for item in facts["state"]["data_tables"].values()))
        return checks

    def prepare(self, opened, variant, facts):
        if variant == "revoked":
            with opened.database.read() as uow:
                proposer = uow.connection.execute("SELECT proposer_agent_id FROM signoffs").fetchone()[0]
            return deny_operation(opened, proposer, "tables_archive")
        return super().prepare(opened, variant, facts)

    def proceed(self, opened, steps, *, repeat=False):
        steps.append({"step": "signoff-worker-pass", "result": opened.signoff_worker_pass()})
        steps.append({"step": "file-recovery-pass", "result": jsonable(opened.file_recovery_pass())})

    def expectations(self, variant, before, after, effects, steps):
        checks = Checks()
        common_checks(checks, before, after)
        signoff, state = one(after, "signoffs")
        receipt = after["state"]["apply_receipts"].get(signoff)
        tables = after["state"]["data_tables"]
        if variant == "none":
            checks("the Signoff left approved and is applied", state["state"] == "applied", state=state)
            checks("its receipt (same id) is applied exactly once", receipt["state"] == "applied" and receipt["has_result"], receipt=receipt)
            checks("the table is archived once", [item["archived"] for item in tables.values()] == [1]
                   and added(before, after, "table_versions") == 1, tables=tables)
            checks("no second receipt was created", added(before, after, "apply_receipts") == 0)
        else:
            checks("the Signoff is refused by the current policy", state["state"] != "applied" and state["error"] == "policy_denied", state=state)
            checks("its receipt did not apply", receipt["state"] != "applied", receipt=receipt)
            checks("nothing was applied", all(item["archived"] == 0 for item in tables.values())
                   and added(before, after, "table_versions") == 0, tables=tables)
        return checks


class S2(Case):
    id = "S2"
    title = "Interrupted document file application with durable verified progress"
    variants = ("none", "revoked", "conflict")
    ORIGINAL = "# First\nOriginal body.\n"
    CHANGED = "# First\nChanged body.\n"
    OUTSIDE = b"Changed outside Qorqut.\n"

    def capture(self, host, fixture):
        from qorqut_operations.contracts import plain

        business, agent = host.ids["first"], host.agent("first")
        created = host.call(agent, "notes_create", {"title": "Plan notes", "body": self.ORIGINAL, "target_business_id": business})
        assert created.status == "ok", created.error
        document = plain(created.data)["document"]
        applier = host.runtime.file_applier
        original = applier.apply

        def apply_then_stop(receipt_id):
            original(receipt_id)
            raise hosts.ProcessStopped()

        applier.apply = apply_then_stop
        try:
            interrupted(lambda: host.call(agent, "notes_update", {"document_id": document["id"], "body": self.CHANGED,
                "target_business_id": business, "expected_content_hash": document["content_hash"]},
                expected_revision=document["revision"]))
        finally:
            applier.apply = original
        return {"document_id": document["id"], "business_id": business, "proposer": host.agent_id(),
                "revision_before": document["revision"], "content_hash_before": document["content_hash"]}

    def _pending(self, facts):
        return rows_of(facts, "apply_receipts", state="applying")

    def preconditions(self, facts, fixture):
        checks = Checks()
        pending = self._pending(facts)
        checks("exactly one receipt is applying", len(pending) == 1, pending=pending)
        receipt = pending[0][1] if pending else {}
        checks("it is an allowed-file receipt (no Signoff) with verified progress",
               any(name != "invocation" for name in receipt.get("progress", [])) and "invocation" in receipt.get("progress", []), receipt=receipt)
        documents = [item for key, item in rows_of(facts, "documents", availability="applying")]
        checks("exactly one document is marked applying with its old revision", len(documents) == 1 and documents[0]["revision"] == 1, documents=documents)
        checks("its claim is retained", facts["counts"].get("resource_claims", 0) >= 1, claims=facts["counts"].get("resource_claims"))
        return checks

    def prepare(self, opened, variant, facts):
        with opened.database.read() as uow:
            row = uow.connection.execute(
                "SELECT d.id, d.path, b.hq_path, r.principal_id FROM apply_receipts r, documents d JOIN businesses b ON b.id=d.business_id "
                "WHERE r.state='applying' AND d.availability='applying'").fetchone()
        if variant == "revoked":
            return deny_operation(opened, row["principal_id"], "notes_update")
        if variant == "conflict":
            target = Path(row["hq_path"]) / row["path"]
            target.write_bytes(self.OUTSIDE)
            return {"how": "managed file overwritten outside Qorqut", "path": str(target), "file": file_state(target)}
        return super().prepare(opened, variant, facts)

    def proceed(self, opened, steps, *, repeat=False):
        steps.append({"step": "file-recovery-pass", "result": jsonable(opened.file_recovery_pass())})
        steps.append({"step": "signoff-worker-pass", "result": opened.signoff_worker_pass()})

    def expectations(self, variant, before, after, effects, steps):
        checks = Checks()
        common_checks(checks, before, after)
        receipt_id, _ = rows_of(before, "apply_receipts", state="applying")[0]
        document, old = rows_of(before, "documents", availability="applying")[0]
        receipt, current, file_after = after["state"]["apply_receipts"][receipt_id], after["state"]["documents"][document], after["files"][document]
        versions = added(before, after, "qbdv_document_versions")
        if variant == "none":
            checks("the receipt finished once and its claim was released", receipt["state"] == "applied"
                   and after["counts"]["resource_claims"] < before["counts"]["resource_claims"], receipt=receipt)
            checks("the document is ready at the next revision with the retained bytes' hash", current["availability"] == "ready"
                   and current["revision"] == old["revision"] + 1 and file_after["sha256"] == current["content_hash"],
                   current=current, file=file_after)
            checks("finalized once: exactly one document version was recorded", versions == 1, versions=versions)
            checks("the retained intended bytes are still the file's bytes (equality proves byte preservation, not the absence of "
                   "an identical rewrite)", file_after == before["files"][document], before=before["files"][document], after=file_after)
        else:
            checks("the receipt did not apply", receipt["state"] != "applied", receipt=receipt)
            checks("the document revision and recorded hash did not move", current["revision"] == old["revision"]
                   and current["content_hash"] == old["content_hash"], current=current)
            checks("no document version was recorded", versions == 0, versions=versions)
            if variant == "conflict":
                checks("the externally changed bytes were not overwritten",
                       file_after["sha256"] == sha256_bytes(self.OUTSIDE), file=file_after)
            else:
                checks("the retained bytes are unchanged (byte equality only)", file_after == before["files"][document], file=file_after)
        return checks


class TableHistory(Case):
    """S3: a real table revision with its captured snapshot and bound publication job."""

    variants = ("none", "revoked")

    def _setup(self, host, fixture):
        details = hosts.initialize_table_history_repository(Path(fixture) / "instance" / OPERATIONS_ROOT)
        from qorqut_operations.contracts import plain

        owner = host.human("first")
        created = plain(host.call(owner, "tables_create", {"title": "Leads", "columns": [{"id": "name", "name": "Name", "type": "text"}]}).data)
        written = plain(host.call(owner, "tables_rows_write", {"table_id": created["table"]["id"],
            "rows": [{"cells": {"name": "Alice"}}]}, expected_revision=created["table"]["revision"]).data)
        return details, owner, created["job_id"], written["job_id"]

    def prepare(self, opened, variant, facts):
        if variant == "revoked":
            return deactivate_worker(opened, "tables.version")
        return super().prepare(opened, variant, facts)

    def proceed(self, opened, steps, *, repeat=False):
        drain_background(opened, steps)

    def exact(self, before, after):
        return {"table_content": content_manifest(after["git"]["operations"]["table_content"])}

    @staticmethod
    def published_content(checks, before, after):
        """Each snapshot the checkpoint saved is, byte-for-byte as JSON, what its commit publishes at the version's path."""
        content = {item["commit"]: item for item in after["git"]["operations"]["table_content"]}
        missing, different = [], []
        for version, snapshot in sorted(before["table_snapshots"].items()):
            commit = after["state"]["table_versions"][version]["git_commit"]
            entry = content.get(commit, {}).get("files", {}).get(snapshot["path"])
            if entry is None:
                missing.append(version)
            elif entry["json_digest"] != snapshot["digest"]:
                different.append(version)
        checks("every saved snapshot is the content its commit publishes at the version path",
               bool(before["table_snapshots"]) and not missing and not different, missing=missing, different=different)

    def _common(self, checks, variant, before, after):
        common_checks(checks, before, after)
        versions = after["state"]["table_versions"]
        content = {item["commit"]: item for item in after["git"]["operations"]["table_content"]}
        changed = [item["commit"] for item in before["git"]["operations"]["table_content"] if content.get(item["commit"]) != item]
        checks("commits already on the branch keep their exact tree content and parents", not changed, changed=changed)
        if variant == "revoked":
            checks("no table version was published", table_commits(after) == table_commits(before), commits=table_commits(after))
            checks("the published table content is unchanged", content_manifest(after["git"]["operations"]["table_content"])
                   == content_manifest(before["git"]["operations"]["table_content"]))
            checks("no further publication job succeeded", len(rows_of(after, "jobs", kind="tables.version", state="succeeded"))
                   == len(rows_of(before, "jobs", kind="tables.version", state="succeeded")))
        return versions


class S3a(TableHistory):
    id = "S3a"
    title = "Table revisions with captured snapshots and queued publication jobs (before Git publication)"

    def capture(self, host, fixture):
        details, owner, first, second = self._setup(host, fixture)
        return {**details, "jobs": [first, second]}

    def preconditions(self, facts, fixture):
        checks = Checks()
        versions = facts["state"]["table_versions"]
        checks("two table versions hold their snapshots and no commit", len(versions) == 2
               and all(item["snapshot_retained"] and item["git_commit"] is None for item in versions.values()), versions=versions)
        checks("two publication jobs are queued and unclaimed", len(rows_of(facts, "jobs", kind="tables.version", state="queued")) == 2)
        checks("nothing is published on the table-history branch", table_commits(facts) == [])
        return checks

    def expectations(self, variant, before, after, effects, steps):
        checks = Checks()
        versions = self._common(checks, variant, before, after)
        if variant == "none":
            commits = [item["git_commit"] for item in versions.values()]
            checks("every version is published exactly once", all(commits) and len(set(commits)) == len(commits) == 2, commits=commits)
            checks("the branch holds exactly one commit per version", len(table_commits(after)) == 2, commits=table_commits(after))
            ordered = [item["git_commit"] for item in sorted(versions.values(), key=lambda item: item["sequence"])]
            checks("publication kept the version order", ordered == [oid for oid, _ in reversed(table_commits(after))], commits=ordered)
            self.published_content(checks, before, after)
            checks("the snapshots were released after publication", not any(item["snapshot_retained"] for item in versions.values()))
            checks("the jobs succeeded", len(rows_of(after, "jobs", kind="tables.version", state="succeeded")) == 2)
        else:
            checks("the snapshots are retained", all(item["snapshot_retained"] for item in versions.values()))
        return checks


class S3b(TableHistory):
    id = "S3b"
    title = "Table version published to Git but not yet recorded in SQL (job running)"

    def capture(self, host, fixture):
        details, owner, first, second = self._setup(host, fixture)
        assert host.run_job(first)["state"] == "succeeded"
        history = host.runtime.domains.tables.history
        real = history.runner

        def runner(command, **options):
            result = real(command, **options)
            if "update-ref" in command and result.returncode == 0:
                raise hosts.ProcessStopped()
            return result

        history.runner = runner
        try:
            interrupted(host.run_job, second)
        finally:
            history.runner = real
        return {**details, "jobs": [first, second]}

    def preconditions(self, facts, fixture):
        checks = Checks()
        versions = facts["state"]["table_versions"]
        pending = [key for key, item in versions.items() if item["git_commit"] is None]
        checks("one version is recorded in SQL, one is not", len(versions) == 2 and len(pending) == 1)
        checks("the unrecorded version still holds its snapshot", all(versions[key]["snapshot_retained"] for key in pending))
        checks("its publication job is running (claimed)", len(rows_of(facts, "jobs", kind="tables.version", state="running")) == 1)
        checks("the branch already holds a commit for every version", len(table_commits(facts)) == 2, commits=table_commits(facts))
        return checks

    def expectations(self, variant, before, after, effects, steps):
        checks = Checks()
        versions = self._common(checks, variant, before, after)
        if variant == "none":
            commits = [item["git_commit"] for item in versions.values()]
            checks("both versions are recorded with distinct commits", all(commits) and len(set(commits)) == 2, commits=commits)
            checks("no second commit was made for the unrecorded version", table_commits(after) == table_commits(before), commits=table_commits(after))
            newest = sorted(versions.values(), key=lambda item: item["sequence"])[-1]
            checks("the unrecorded version was recorded with the commit already on the branch",
                   newest["git_commit"] == table_commits(before)[0][0], recorded=newest["git_commit"], branch=table_commits(before)[0])
            checks("the recorded commits are exactly the branch's", sorted(commits) == sorted(item[0] for item in table_commits(after)))
            self.published_content(checks, before, after)
            checks("both jobs are now succeeded", len(rows_of(after, "jobs", kind="tables.version", state="succeeded")) == 2)
        else:
            checks("the unrecorded version stays unrecorded and keeps its snapshot",
                   [item["git_commit"] for item in versions.values()].count(None) == 1)
        return checks


class ContextCase(Case):
    """S4: Context queue work (input, bound filing job, Git event, SQL completion)."""

    variants = ("none", "revoked")
    volatile = {"documents": ("content_hash",)}      # the filed source carries the event identifier the engine drew at random

    def _upload(self, host):
        """The stock upload operation: a document and version, one filing, one registered job and one queued input."""
        data = host.upload(host.agent(), "Met Alice about the pilot.", title="Pilot call")
        return data["document"], data["job"]["id"]

    def prepare(self, opened, variant, facts):
        if variant == "revoked":
            return deactivate_agent(opened, agent_by_name(opened.fixture))
        return super().prepare(opened, variant, facts)

    def settle(self, opened, steps):
        opened.advance(seconds=16)
        drain_background(opened, steps, label="filing-job")

    @staticmethod
    def completion_binding(checks, name, before, after, *, event, commit):
        """The single completion binds the saved input, its filing, bound job and source version to ``event`` and the Context
        commit that publishes it. Everything is read from the stored rows, nothing is trusted from a step result."""
        (input_id,) = before["context"]["inputs"]
        filing_id, filing = next((key, item) for key, item in before["context"]["filings"].items() if item["input_id"] == input_id)
        completions = after["context"]["completions"]
        expected = {"input_id": input_id, "filing_id": filing_id, "job_id": filing["job_id"],
                    "source_version_id": filing["source_version_id"], "context_event_id": event, "published_commit": commit}
        found = [{key: item[key] for key in expected} for item in completions]
        checks(name, found == [expected], expected=expected, found=found)
        checks("the filings (input, job, source version bindings) are unchanged",
               after["context"]["filings"] == before["context"]["filings"])


class S4a(ContextCase):
    id = "S4a"
    title = "Context input queued with its bound filing job (no model call yet)"

    def capture(self, host, fixture):
        document, job = self._upload(host)
        return {"document_id": document["id"], "job_id": job, "model_calls": len(host.runner.calls)}

    def preconditions(self, facts, fixture):
        checks = Checks()
        checks("one queued input and no attempt", len(rows_of(facts, "context_inputs", state="queued")) == 1
               and facts["counts"]["context_input_attempts"] == 0)
        checks("one filing is bound to a queued job", facts["counts"]["context_filings"] == 1
               and len(rows_of(facts, "jobs", kind="context.file", state="queued")) == 1)
        checks("no completion and nothing published", facts["counts"]["context_completions"] == 0)
        return checks

    def proceed(self, opened, steps, *, repeat=False):
        steps.append({"step": "context-recover", "result": jsonable(opened.context_recover())})
        for index in range(4):
            result = opened.context_step()
            steps.append({"step": f"context-step:{index + 1}", "result": jsonable(result)})
            if result.get("status") in ("idle", "not_ready"):
                break
        self.settle(opened, steps)

    def expectations(self, variant, before, after, effects, steps):
        checks = Checks()
        common_checks(checks, before, after)
        if variant == "none":
            context = after["git"]["context"]
            (input_id,) = before["context"]["inputs"]
            event = after["context"]["inputs"][input_id]["event_id"]
            checks("the model ran exactly once for the input", effects["model_propagations"] == 1, calls=effects["model_calls"])
            checks("the newly published event is the HEAD commit of the Context graph", event is not None
                   and context["head"] != before["git"]["context"]["head"] and context["log"][0][0] == context["head"]
                   and event in context["log"][0][1], head=context["log"][:1], event=event)
            self.completion_binding(checks, "the completion binds the input, filing, job and source version to the new event and its HEAD commit",
                                    before, after, event=event, commit=context["head"])
            checks("the input is published and completed once", len(rows_of(after, "context_inputs", state="published")) == 1
                   and len(rows_of(after, "context_completions")) == 1, state=after["state"].get("context_inputs"))
            checks("the bound job succeeded", len(rows_of(after, "jobs", kind="context.file", state="succeeded")) == 1)
            checks("one Context event was published", after["git"]["context"]["commits"] == before["git"]["context"]["commits"] + 1,
                   before=before["git"]["context"]["commits"], after=after["git"]["context"]["commits"])
        else:
            checks("no model ran and nothing was published", effects["model_propagations"] == 0
                   and after["git"]["context"]["commits"] == before["git"]["context"]["commits"], calls=effects["model_calls"])
            checks("the input stays queued without a completion", len(rows_of(after, "context_inputs", state="queued")) == 1
                   and not rows_of(after, "context_completions"))
        return checks


class S4b(ContextCase):
    id = "S4b"
    title = "Context event published to Git but its SQL completion missing (no repeated model call)"

    def capture(self, host, fixture):
        import sqlite3

        from qorqut_operations.context_queue import transitions

        document, job = self._upload(host)
        driver, write, failures = host.driver, host.driver._write, []

        def flaky(function, **arguments):
            if function is transitions.record_published and not failures:
                failures.append(1)
                raise sqlite3.OperationalError("disk I/O error")
            return write(function, **arguments)

        driver._write = flaky
        outcome = host.step()
        driver._write = write
        assert outcome["status"] == "unrecorded" and outcome["reason"] == "record_failed", outcome
        return {"document_id": document["id"], "job_id": job, "step": jsonable(outcome), "model_calls": len(host.runner.calls),
                "model_propagations": host.runner.propagations}

    def preconditions(self, facts, fixture):
        checks = Checks()
        checks("the input is still processing", len(rows_of(facts, "context_inputs", state="processing")) == 1)
        checks("no completion is recorded", facts["counts"]["context_completions"] == 0)
        checks("the event is already in the Context graph", facts["git"]["context"]["commits"] >= 2, commits=facts["git"]["context"]["commits"])
        checks("the filing job is still queued", len(rows_of(facts, "jobs", kind="context.file", state="queued")) == 1)
        return checks

    def proceed(self, opened, steps, *, repeat=False):
        steps.append({"step": "context-recover", "result": jsonable(opened.context_recover())})
        steps.append({"step": "context-step", "result": jsonable(opened.context_step())})
        self.settle(opened, steps)

    def exact(self, before, after):
        context = after["git"]["context"]
        return {"context": {"head": context["head"], "tree": context["tree"], "refs": context["refs"], "log": context["log"],
                            **after["context"]}}

    def expectations(self, variant, before, after, effects, steps):
        """Already-published work: recovery must reuse the saved event and its publication commit, in both variants (with the
        coordinator retired the baseline still completes mechanically; it does not refuse)."""
        checks = Checks()
        common_checks(checks, before, after)
        saved, now, git_before, git_after = before["context"], after["context"], before["git"]["context"], after["git"]["context"]
        (input_id,) = saved["inputs"]
        event = saved["inputs"][input_id]["event_id"]
        publication = next((oid for oid, subject in git_before["log"] if event and event in subject), None)
        checks("the checkpoint already holds the event, published as the Context HEAD commit", event is not None
               and publication is not None and publication == git_before["head"], event=event, head=git_before["head"])
        checks("recovery made no second model call", effects["model_propagations"] == 0, calls=effects["model_calls"])
        checks("the input keeps its saved event id and no attempt was added or rebound",
               now["inputs"].get(input_id) == saved["inputs"][input_id] and now["attempts"] == saved["attempts"],
               saved=saved["attempts"], now=now["attempts"])
        checks("Context HEAD, refs, tree and history are unchanged (no second event)", git_after["head"] == git_before["head"]
               and git_after["refs"] == git_before["refs"] and git_after["tree"] == git_before["tree"]
               and git_after["log"] == git_before["log"], before=git_before["head"], after=git_after["head"])
        self.completion_binding(checks, "the completion binds the original input, filing, job and source version to the original event "
                                "and its actual publication commit", before, after, event=event, commit=publication)
        recovery = next((item["result"] for item in steps if item["step"] == "context-recover"), {})
        checks("the recovery step reports that same event and publication commit as recovered", recovery.get("recovered") is True
               and recovery.get("context_event_id") == event and recovery.get("published_commit") == publication, recovery=recovery)
        if variant == "none":
            checks("the missing completion was recorded exactly once", len(rows_of(after, "context_completions")) == 1
                   and len(rows_of(after, "context_inputs", state="published")) == 1, state=after["state"].get("context_completions"))
            checks("the bound job succeeded", len(rows_of(after, "jobs", kind="context.file", state="succeeded")) == 1)
        return checks


CASES = {case.id: case for case in (S1(), S2(), S3a(), S3b(), S4a(), S4b())}
