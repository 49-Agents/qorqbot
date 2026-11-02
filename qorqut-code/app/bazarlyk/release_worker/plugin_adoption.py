"""Receipt-backed plugin publication, pin adoption and preparation recipe.

Invoked by release-worker after deployment. Selection, profile revisions and
capability additions are frozen in the release plan. No terminal is restarted.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from bazarlyk.release_worker.adapters import _tree_digest
from bazarlyk.release_worker.errors import ReleaseError
from bazarlyk.release_worker.io import atomic_json
from bazarlyk.release_worker.manifest import load_plan


PROJECTIONS = (".bazarlyk/registration.json", ".claude/settings.json", ".mcp.json",
               "CLAUDE.md", "AGENTS.md", ".codex/config.toml")


def plugin_changes(profile, artifact):
    matches = [p for p in profile.get("plugins", []) if p["id"] == artifact["id"]]
    if len(matches) != 1:
        raise ReleaseError("selected agent must already have exactly one plugin pin")
    return {"plugins": [{**p, **artifact} if p["id"] == artifact["id"] else p
                        for p in profile["plugins"]]}


def adopt(plan_path, component_id, artifact_path, runtime_path, receipt_path):
    from bazarlyk.bootstrap.preparation import installed_versions, prepare_agent
    from bazarlyk.bootstrap.delegated_runtime import supported_cli_version
    from bazarlyk.catalog.packages import load_package
    from bazarlyk.catalog.sources import load_sources
    from qorqut_operations.contracts import InvocationOptions, Principal
    from bazarlyk.service.runtime import build_runtime
    from bazarlyk.transport.auth import Authentication

    plan = load_plan(plan_path)
    record = json.loads(plan_path.with_name("record.json").read_text())
    if record.get("approvals", {}).get("adopt", {}).get("plan_digest") != plan["plan_digest"]:
        raise ReleaseError("plugin adoption requires the frozen plan's recorded authorization")
    component = next(c for c in plan["components"] if c["id"] == component_id)
    if component["category"] != "plugin":
        raise ReleaseError("this recipe only adopts plugins")
    selected = component["adoption"]["agents"]
    recipe = component["adoption_recipe"]
    expected = recipe["profiles"]
    if set(selected) != set(expected) or len(selected) != len(set(selected)):
        raise ReleaseError("frozen profile selection does not match selected agents")
    from .adoption_runtime import resolve
    workspace, interpreter = resolve(plan, recipe, runtime_path)
    root = Path(recipe["businesses_root"])
    runtime = build_runtime(root, workspace, worker_profiles=[])
    package = load_package(artifact_path, component["manifest"], "plugin", runtime.registry)
    kind, mapped_source, mapped_manifest = load_sources(workspace)[component_id]
    mapped = load_package(mapped_source, mapped_manifest, kind, runtime.registry)
    if package.id != component_id or package.ref != mapped.ref:
        raise ReleaseError("live publication source does not match the transferred plugin artifact")
    versions = installed_versions()
    journal_path = receipt_path.with_suffix(".journal.json")
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        if journal.get("plan_digest") != plan["plan_digest"] or journal.get("component") != component_id:
            raise ReleaseError("adoption journal belongs to another release")
    else:
        journal = {"plan_digest": plan["plan_digest"], "component": component_id, "agents": {}}
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with runtime.database.read() as uow:
        owner, _, _ = Authentication(runtime.database, root).installation_owner(connection=uow.connection)
        owner_ctx = runtime.policy.context_for(owner.id, business_id=None, review_business_id=None, uow=uow)
        agents = []
        for agent_id in selected:
            row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None or row["status"] != "active":
                raise ReleaseError("selected agent is absent or inactive")
            profile = json.loads(row["profile_json"])
            if sum(p["id"] == component_id for p in profile.get("plugins", [])) != 1:
                raise ReleaseError("selected agent no longer has the plugin")
            registration = uow.connection.execute("SELECT * FROM machine_registrations WHERE principal_id=?", (row["principal_id"],)).fetchone()
            if registration is None or registration["service_url"] != expected[agent_id]["service_url"]:
                raise ReleaseError("selected agent registration changed")
            prior = journal["agents"].get(agent_id)
            expected_revision = prior["revision"] if prior and prior.get("status") in {"updated", "prepared"} else expected[agent_id]["revision"]
            if row["revision"] != expected_revision:
                raise ReleaseError("selected profile revision changed; reconcile before continuing")
            changes = plugin_changes(profile, package.ref)
            workspace = Path(row["workspace"])
            if not workspace.is_absolute():
                workspace = root / workspace
            registration_file = workspace / ".bazarlyk/registration.json"
            capture = json.loads(registration_file.read_text()).get("capture_transcripts")
            if type(capture) is not bool:
                raise ReleaseError("transcript capture setting must be explicit")
            agents.append((dict(row), profile, changes, workspace, capture, registration["service_url"]))
    # Preflight every selected profile before the first mutation.
    atomic_json(journal_path, journal)
    experience_receipt = None
    backup_receipt = None
    if component.get("experience_binding"):
        from .experience_binding import bind_experiences
        experience_receipt = bind_experiences(plan, component, artifact_path)
    if plan["installation"].get("session_backup"):
        from .session_backup import bind_backup
        backup_receipt = bind_backup(plan)
    published = runtime.application.invoke_or_raise(owner_ctx, "plugins_publish",
        {"source_id": component_id, "rationale": "Owner-approved plugin release"},
        InvocationOptions(idempotency_key=f"{plan['release_id']}:plugin:{component_id}:publish"))
    if published.status != "ok" or published.data["artifact"] != package.ref:
        raise ReleaseError("canonical plugin publication did not return the exact artifact")
    journal["published"] = package.ref
    atomic_json(journal_path, journal)
    for row, profile, changes, workspace, capture, endpoint in agents:
        agent_id = row["agent_id"]
        prior = journal["agents"].get(agent_id, {})
        if prior.get("status") == "prepared":
            continue
        if prior.get("status") != "updated":
            backup = receipt_path.parent / "profile-backups" / agent_id
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_json(backup / "profile.json", profile)
            for relative in PROJECTIONS:
                source = workspace / relative
                if source.is_file() and not source.is_symlink():
                    destination = backup / relative
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(source, destination)
                    destination.chmod(0o600)
            context = owner_ctx if row["home_business_id"] is None else owner_ctx.select_business(row["home_business_id"])
            updated = runtime.application.invoke_or_raise(context, "agents_update",
                {"agent_id": agent_id, "changes": changes, "rationale": "Owner-approved plugin release"},
                InvocationOptions(expected_revision=row["revision"], idempotency_key=f"{plan['release_id']}:plugin:{component_id}:{agent_id}"))
            if updated.status != "ok":
                raise ReleaseError("canonical agent update did not apply")
            with runtime.database.read() as uow:
                revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()[0]
            journal["agents"][agent_id] = {"status": "updated", "revision": revision}
            atomic_json(journal_path, journal)
        prepare_agent(runtime.resolver, Principal(agent_id, "agent"), endpoint=endpoint,
                      versions=versions, interpreter=interpreter, capture_transcripts=capture)
        with runtime.database.read() as uow:
            after = uow.connection.execute("SELECT revision,profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
            registration = uow.connection.execute("SELECT profile_revision FROM machine_registrations WHERE principal_id=?", (row["principal_id"],)).fetchone()
        current = json.loads(after["profile_json"])
        if not any(all(p.get(k) == v for k, v in package.ref.items()) for p in current["plugins"]) or registration[0] != after["revision"]:
            raise ReleaseError("prepared registration does not match adopted plugin")
        journal["agents"][agent_id] = {"status": "prepared", "revision": after["revision"]}
        atomic_json(journal_path, journal)
    atomic_json(receipt_path, {"schema_version": 1, "operation": "adopt", "release_id": plan["release_id"],
        "plan_digest": plan["plan_digest"], "component": component_id, "source_commit": component["source_commit"],
        "outcome": "succeeded", "release": str(artifact_path), "artifact_digest": _tree_digest(artifact_path),
        "agents": sorted(selected), "plugin": package.ref, "journal": str(journal_path),
        "experience_binding": experience_receipt, "session_backup": backup_receipt})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "artifact", "runtime", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--component", required=True)
    args = parser.parse_args()
    adopt(args.plan, args.component, args.artifact, args.runtime, args.receipt)


if __name__ == "__main__":
    main()
