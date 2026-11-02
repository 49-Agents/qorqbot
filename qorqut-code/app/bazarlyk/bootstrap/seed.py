"""One-time local preparation helpers for newly registered businesses only.

A business owns resources (files, records, integrations), never resident agents:
seeding creates no agent profile, plan, membership or preparation, and needs no
published business role.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

import yaml

from bazarlyk.bootstrap.client import path_has_symlink, service_url
from bazarlyk.composition.assembly import host_assembly, open_database
from bazarlyk.composition.locations import business_hq, business_root, seed_marker
from bazarlyk.catalog.packages import content_hash
from bazarlyk.integrations import registrations as outreach_registrations
from qorqut_operations.contracts import DomainError, Principal, new_id
from bazarlyk.storage import history
from qorqut_operations.storage.files import FileApplier
from qorqut_operations.storage.receipts import ApplyReceipts
from bazarlyk.sandbox.state import SandboxStateStore
from bazarlyk.sandbox.config import creation_enabled
from .preparation import _write_file

ROOT_ORIENTATION = """# Businesses Workspace

Prepared global agent folders receive their current identity, scope, role, and
core facts from the shared Qorqut bootstrap. Businesses hold files and records,
not resident agents. The application owns operational records and current
configuration. Authored context, notes, code, and media remain files. Generated
YAML and Claude settings are projections. Report a missing bootstrap and avoid
business work until preparation succeeds.
"""


# Global authored shared instructions live under operations/files; the published Context graph is operations/context-graph (engine-owned).
INSTRUCTIONS = "files/instructions"


def register_context_sources(database, paths, *, template_root: Path, business_id: str | None = None, hq: Path | None = None):
    """Register verified authored sources once; existing indexed bytes must agree."""
    if business_id:
        items = [(key, f"context/{key}.md", template_root / f"context/{key}.md") for key in ("product", "icp", "brand", "positioning")]
        base = Path(hq)
    else:
        items = [(key, f"{INSTRUCTIONS}/{relative}", template_root / "platform-context" / relative) for key, relative in (
            ("foundation", "agent-foundation.md"),
            ("ecosystem-agent", "operating/ecosystem-agent.md"), ("ecosystem", "ecosystem.md"))]
        base = paths.operations
    result = []
    for key, relative, source in items:
        path = base / relative
        if not path.exists():
            _write_file(path, source.read_bytes())
        body = path.read_bytes()
        digest = content_hash(body)
        with database.transaction() as uow:
            repository = uow.repository("documents", business_id=business_id)
            existing = repository.list(filters={"namespace": business_id or "global", "path": relative}, limit=1)
            if existing:
                row = existing[0]
                if row["content_hash"].removeprefix("sha256:") != digest.removeprefix("sha256:") or row["metadata_json"].get("bootstrap_key") != key:
                    raise DomainError("document_conflict", "Registered context requires explicit reconciliation before seeding")
            else:
                row = repository.insert({"id": new_id(), "namespace": business_id or "global", "path": relative,
                    "kind": "context" if business_id else "shared_instruction", "content_hash": digest,
                    "metadata_json": {"bootstrap_key": key, "authored": True}})
                history.seed_document(uow.connection, row, body, author="system:register_context_sources",
                                      operation="register_context_sources")
            result.append(row)
    return result


def apply_initial_plan(database, ctx, plan, operations, file_resolver, locks):
    """Trusted one-time seed application through the shared durable file protocol."""
    with database.transaction() as uow:
        receipt = ApplyReceipts(uow).create(plan)
    FileApplier(database, file_resolver, locks=locks).apply(receipt["id"])
    with database.transaction() as uow:
        result = operations.apply(ctx, plan, uow)
        ApplyReceipts(uow).finish(receipt["id"], result)
    return result


def _copy_missing(source: Path, destination: Path):
    """Populate a new seed workspace without replacing any existing bytes."""
    if path_has_symlink(source) or path_has_symlink(destination):
        raise DomainError("bootstrap_conflict", "Business templates cannot cross symbolic links")
    if source.is_dir():
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise DomainError("bootstrap_conflict", "Business bootstrap path is occupied by an unsafe type")
        else:
            destination.mkdir(mode=0o755)
        for child in source.iterdir():
            _copy_missing(child, destination / child.name)
        return
    if not source.is_file():
        raise DomainError("bootstrap_conflict", "Business template contains an unsupported path")
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise DomainError("bootstrap_conflict", "Business bootstrap file is occupied by an unsafe type")
        return
    shutil.copy2(source, destination)


def _materialize_seed_workspace(resolver, *, template_root, business, owner):
    """Idempotently finish post-commit local business files.

    The database creation aggregate is intentionally complete before this runs.
    A retry may only resume a workspace bearing this business's private bootstrap
    marker; it never adopts an arbitrary occupied business directory.
    """
    hq = business_hq(resolver.paths, business)
    marker = seed_marker(resolver.paths, business["id"])
    if path_has_symlink(hq) or path_has_symlink(marker):
        raise DomainError("bootstrap_conflict", "Business bootstrap paths cannot cross symbolic links")
    if hq.exists() and not marker.exists():
        raise DomainError("bootstrap_conflict", "Registered new business workspace is occupied without its bootstrap marker")
    if marker.exists():
        try:
            saved = json.loads(marker.read_text())
        except (OSError, ValueError) as exc:
            raise DomainError("bootstrap_conflict", "Business bootstrap marker is invalid") from exc
        if saved != {"business_id": business["id"]}:
            raise DomainError("bootstrap_conflict", "Business bootstrap marker belongs to another business")
    else:
        _write_file(marker, json.dumps({"business_id": business["id"]}, sort_keys=True).encode(), private=True)
    # The marker lives outside hq and is written first, so a crash cannot leave a
    # populated hq that looks like an unadoptable legacy workspace.
    hq.mkdir(parents=True, exist_ok=True)
    excluded = {".git", "agents", "platform-context", "tests", "bootstrap.sh", "BOOTSTRAP.md", "landing", "business.yaml", "__pycache__"}
    for source in Path(template_root).iterdir():
        if source.name in excluded:
            continue
        _copy_missing(source, hq / source.name)
    register_context_sources(resolver.database, resolver.paths, template_root=template_root)
    register_context_sources(resolver.database, resolver.paths, template_root=template_root, business_id=business["id"], hq=hq)
    if not (resolver.paths.operations / "CLAUDE.md").exists():
        _write_file(resolver.paths.operations / "CLAUDE.md", ROOT_ORIENTATION.encode())
    export = {"schema_version": 2, "generated": True, "business_id": business["id"], "configuration_revision": business["revision"],
              "name": business["name"], "slug": business["slug"], "stage": business["stage"], "timezone": business["timezone"],
              "one_liner": business["config_json"]["one_liner"], "owner_id": owner.id, "code": business["config_json"].get("code", [])}
    _write_file(hq / "business.yaml", yaml.safe_dump(export, sort_keys=False).encode())
    # Its absence means ordinary future seed calls take the regular idempotent
    # route.  A failure before this point retains the exact marker needed for
    # this function's narrow recovery path.
    marker.unlink()


def _seed_result(business_id, *, imported):
    """Businesses own resources only; no agent is created or prepared."""
    return {"business_id": business_id, "imported": imported}


def seed_business(resolver, *, template_root: Path, slug: str, name: str, description: str,
                  owner: Principal, code_repositories: list[dict] | None = None,
                  github_provisioning: dict | None = None) -> dict:
    """Create a new local business. Existing DB authority is never reimported from YAML."""
    if not slug or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in slug) or not slug[0].isalpha():
        raise DomainError("validation_error", "Business slug must be lowercase letters, digits, and hyphens")
    if owner.kind != "human" or not owner.active:
        raise DomainError("forbidden", "Business bootstrap requires an active human owner")
    template_root = Path(template_root)
    selected = {"slug": slug}
    hq = business_hq(resolver.paths, selected)
    with resolver.database.read() as uow:
        existing = uow.connection.execute("SELECT * FROM businesses WHERE slug=?", (slug,)).fetchone()
    if existing:
        with resolver.database.read() as uow:
            membership = uow.repository("business_memberships", business_id=existing["id"]).get(owner.id)
        if not membership or not membership["is_owner"] or not membership["active"]:
            raise DomainError("bootstrap_conflict", "Existing business requires its registered owner")
        configured = json.loads(existing["config_json"]).get("code", [])
        missing_repositories = [item for item in code_repositories or [] if not any(old.get("repo") == item.get("repo") for old in configured)]
        if missing_repositories:
            raise DomainError("repository_registration_required", "Repositories were provisioned; register their metadata through business_config_update before preparing this existing business", details={"code": missing_repositories})
        if github_provisioning is not None and json.loads(existing["config_json"]).get("github_provisioning") != github_provisioning:
            raise DomainError("repository_registration_required", "Repository provisioning intent requires explicit registration")
        # A new creation can be interrupted after its aggregate commit and before
        # local projection. Its exact bootstrap marker permits this narrow
        # resumption, including when sandbox hosting is explicitly disabled.
        marker = seed_marker(resolver.paths, existing["id"])
        if marker.exists() or not hq.exists():
            resumed_business = {**dict(existing), "config_json": json.loads(existing["config_json"])}
            _materialize_seed_workspace(resolver, template_root=template_root, business=resumed_business, owner=owner)
        return _seed_result(existing["id"], imported=False)
    if hq.exists():
        raise DomainError("bootstrap_conflict", "Existing business files require the reviewed migration importer")
    business_id = new_id()
    sandbox_selected = creation_enabled(resolver.paths)
    with resolver.database.transaction() as uow:
        if not uow.repository("principals").get(owner.id):
            uow.repository("principals").insert({"id": owner.id, "kind": "human", "display_name": owner.display_name})
        business = uow.repository("businesses").insert({"id": business_id, "slug": slug, "workspace": str(business_root(resolver.paths, selected)), "hq_path": str(hq),
            "name": name, "stage": "idea", "timezone": "UTC", "config_json": {"one_liner": description,
            "code": code_repositories or [], **({"github_provisioning": github_provisioning} if github_provisioning else {})}})
        uow.repository("business_memberships", business_id=business_id).insert({"principal_id": owner.id, "is_owner": True})
        # Every active global reaches the new business at once: no profile or membership
        # write. The Outreach registration is queued here and sent only after this commit.
        outreach_registrations.queue(uow, business_id)
        # New creation selects sandbox provisioning only through the explicit
        # private host setting.  Existing installations remain legacy even if a
        # host later enables the feature; explicit adoption uses lifecycle.
        if sandbox_selected:
            SandboxStateStore().insert_intent(uow, business_id)
    _materialize_seed_workspace(resolver, template_root=template_root, business=business, owner=owner)
    return _seed_result(business_id, imported=True)


def main():
    from bazarlyk.installation import component
    from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
    parser = argparse.ArgumentParser(description="One-time local business preparation")
    parser.add_argument("--workspace", required=True, type=Path)
    add_instance_arguments(parser)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--owner-id")
    parser.add_argument("--owner-name", required=True)
    # The explicit service endpoint stays part of the bootstrap contract. Seeding prepares no agent, so it is only validated.
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--github-org")
    parser.add_argument("--no-landing", action="store_true")
    parser.add_argument("--app", action="store_true")
    args = parser.parse_args()
    try:
        service_url(args.service_url)
        paths = instance_paths_from_arguments(args, required=("app", "template"))
        template_root = component(paths, "template")
        from bazarlyk.service.changes import ChangeFeed
        database = open_database(paths, host_assembly(ChangeFeed()))
        from bazarlyk.service.runtime import build_runtime
        runtime = build_runtime(args.workspace, paths=paths, database=database)
        runtime.require_authority()
        resolver = runtime.resolver
        from bazarlyk.transport.auth import Authentication
        owner, _, _ = Authentication(database, paths).installation_owner()
        if args.owner_id and args.owner_id != owner.id:
            raise DomainError("forbidden", "Local provisioning requires the registered installation owner")
        # GitHub is an explicit external effect.  Save its deterministic desired
        # bindings with the local business aggregate first, then provision after
        # that commit.  A failed remote call leaves a retryable local request;
        # it cannot leave remote repositories for an unregistered business.
        code_repositories = None
        github_provisioning = None
        if args.github_org:
            from .provision import planned_provisioning
            github_provisioning = planned_provisioning(org=args.github_org, landing=not args.no_landing, app=args.app)
            code_repositories = github_provisioning["code"]
        result = seed_business(resolver, template_root=template_root, slug=args.slug, name=args.name,
            description=args.description, owner=owner,
            code_repositories=code_repositories, github_provisioning=github_provisioning)
        if args.github_org:
            from .provision import provision_repositories
            provisioning = provision_repositories(org=args.github_org, business_path=business_root(paths, {"slug": args.slug}),
                name=args.name, description=args.description, template_root=template_root,
                landing=not args.no_landing, app=args.app, approved_provisioning=github_provisioning)
            from .provision import publish_initial_hq
            publish_initial_hq(Path(provisioning["hq"]), name=args.slug)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DomainError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
