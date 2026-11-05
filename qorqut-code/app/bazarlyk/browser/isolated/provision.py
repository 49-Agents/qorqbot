"""Owner-run provisioning for the isolated desktop browser; reusable for any explicit business.

Idempotent and recoverable. It writes only private files (credentials, VNC password, a copy of the prior
browser block, the credential-free setup projection) and asks the owner API for the one canonical
configuration change at the current revision with a deterministic idempotency key. It never edits the
database, starts a unit, or rotates existing credentials. ``dry_run`` and ``check`` write nothing and take
no lock. A real run holds the per-business provisioning lock that the legacy command shares, across the
authoritative state read, the private files, the canonical application and the projection.
"""

import json
from pathlib import Path
import time
import uuid

from bazarlyk.bootstrap.client import path_has_symlink, write_json_private
from qorqut_operations.contracts import DomainError, InvocationOptions, plain
from .. import inventory
from ..config import ISOLATED, check_identity, configured_target, validate_exit_ip, validate_isolated_proxy
from ..provision import owner_context, provisioning_lock, split_proxy
from . import setup
from .deployment import Binaries, load

INELIGIBLE = "This proxy IP is not confirmed available"


def _idempotency_key(business_id, server, exit_ip, revision):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"qorqut-isolated-browser:{business_id}:{server}:{exit_ip}:{revision}"))


def _same_assignment(current, server, exit_ip):
    return bool(current and current.get("mode") == ISOLATED and current.get("proxy_server") == server
                and current.get("expected_exit_ip") == exit_ip and current.get("enabled") is True)


def record_prior(path, business_id, row):
    """Keep the exact pre-change block and revision as private evidence for recovery.

    This is a snapshot, not a request: the settings API refuses the old block sent back unchanged (its
    generated fields and generation are forbidden, and a missing mode inherits the isolated one). Recovery
    replaces only ``browser`` with the explicit operator-settable values.
    """
    path = Path(path)
    value = {"business_id": business_id, "revision": row["revision"], "browser": plain(row["config_json"].get("browser")),
             "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if path_has_symlink(path):
        raise DomainError("invalid_configuration", "The prior-configuration record cannot follow symlinks")
    if path.exists():
        return False  # The first record is the recovery point and is never replaced.
    write_json_private(path, value)
    return True


def _ineligible(report):
    raise DomainError("configuration_conflict", INELIGIBLE + "; supply a fresh sanitized --inventory snapshot with a clean "
                      "result, persona_assigned=false and no other project assignment. HN alone is allowed; "
                      "unknown is not free.", details=report["eligibility"])


def _assess(runtime, business_id, slug, server, exit_ip, snapshot, now, dry_run):
    """The authoritative current state and the eligibility verdict for these exact inputs."""
    with runtime.database.read() as uow:
        row = uow.repository("businesses").get(business_id, required=True)
    current = row["config_json"].get("browser")
    already = _same_assignment(current, server, exit_ip)
    verdicts = inventory.report(runtime.database, snapshot, sorted({exit_ip, server.split("//")[1].rsplit(":", 1)[0]}),
                                business_id=business_id, now=now)
    report = {"business_id": business_id, "slug": slug, "mode": ISOLATED, "proxy": server, "expected_exit_ip": exit_ip,
              "already_assigned": already, "eligibility": {ip: item["status"] for ip, item in verdicts.items()},
              "will_write": not dry_run, "started": False}
    return row, current, already, all(item["status"] == "available" for item in verdicts.values()), report


def provision_isolated(runtime, slug, proxy_url, *, expected_exit_ip, deployment_path=None, inventory_path=None,
                       prior_path=None, dry_run=False, now=None):
    runtime.require_authority()
    server, credentials = split_proxy(proxy_url)
    server, exit_ip = validate_isolated_proxy(server), validate_exit_ip(expected_exit_ip)
    if not credentials["username"]:
        raise DomainError("validation_error", "The isolated browser needs an authenticated HTTP proxy")
    business_id, ctx = owner_context(runtime, slug)
    deployment = load(deployment_path) if deployment_path else None
    if deployment and deployment.business_id != business_id:
        raise DomainError("configuration_conflict", "The deployment file names a different project")
    if deployment and deployment.paths.instance_root != runtime.paths.instance_root:
        raise DomainError("configuration_conflict", "The deployment file names a different instance root")
    snapshot = inventory.load_snapshot(inventory_path) if inventory_path else None
    # An early read refuses cleanly before anything (even the lock file) is created; it decides nothing.
    _row, _current, already, eligible, report = _assess(runtime, business_id, slug, server, exit_ip, snapshot, now, dry_run)
    if not already and not eligible:
        if dry_run:
            report["blocked"] = INELIGIBLE
            return report
        _ineligible(report)
    if dry_run:
        # Read-only and lock-free: it creates no directory, lock or file and only reports what a run would do.
        report["would"] = ["store isolated credentials", "ensure VNC password files" if deployment else "skip VNC files (no deployment)",
                           "apply browser assignment" if not already else "keep the saved assignment", "write hq/browser/setup.json"]
        return report
    with provisioning_lock(runtime.paths, business_id):
        # Everything below is decided from state read while holding the lock, not from anything read earlier.
        row, current, already, eligible, report = _assess(runtime, business_id, slug, server, exit_ip, snapshot, now, False)
        if current and current.get("mode") == ISOLATED and not already:
            raise DomainError("configuration_conflict", "This project already has a different isolated browser assignment; "
                              "changing it needs an explicit operator rotation, not provisioning")
        if not already and not eligible:
            _ineligible(report)
        setup.store_credentials(runtime.paths, business_id, {"mode": ISOLATED}, credentials)
        if deployment:
            setup.ensure_vnc_password(deployment)
        if prior_path and current is not None and not already:
            record_prior(prior_path, business_id, row)
        if not already:
            proposal = {"mode": ISOLATED, "enabled": True, "proxy_server": server, "expected_exit_ip": exit_ip}
            result = runtime.application.invoke(ctx, "business_config_update", {"config": {**row["config_json"], "browser": proposal}},
                                                InvocationOptions(expected_revision=row["revision"],
                                                                  idempotency_key=_idempotency_key(business_id, server, exit_ip, row["revision"])))
            if result.status != "ok":
                raise DomainError("provisioning_incomplete", "Config was not confirmed; protected credentials retained. Inspect config before retrying")
        with runtime.database.read() as uow:
            saved = uow.repository("businesses").get(business_id, required=True)
        browser = configured_target(saved, runtime.paths)
        if browser.get("mode") != ISOLATED or browser["proxy_server"] != server or browser["expected_exit_ip"] != exit_ip:
            raise DomainError("configuration_conflict", "Browser assignment changed during provisioning; inspect before proceeding")
        report["projection_written"] = setup.write_projection(saved, runtime.paths, deployment)
        report["browser"] = {key: browser[key] for key in ("mode", "enabled", "cdp_port", "generation", "expected_exit_ip")}
        return report


def check_isolated(runtime, slug, deployment_path, *, binaries=Binaries(), x_directory="/tmp", siblings=()):
    """Read-only: is this business fully provisioned and is the host ready to start its two units?"""
    business_id, _ctx = owner_context(runtime, slug)
    deployment = load(deployment_path)
    if deployment.business_id != business_id or deployment.paths.instance_root != runtime.paths.instance_root:
        raise DomainError("configuration_conflict", "The deployment file does not belong to this project and instance")
    with runtime.database.read() as uow:
        row = uow.repository("businesses").get(business_id, required=True)
    findings = setup.preflight(deployment, row, binaries=binaries, x_directory=x_directory,
                               siblings=[load(path) for path in siblings])
    try:
        expected = setup.projection(row, runtime.paths, deployment)
        target = Path(expected["paths"]["setup"])
        current = json.loads(target.read_text()) if target.exists() else None
        findings.append({"check": "setup projection", "ok": current == expected,
                         "detail": "" if current == expected else "missing or stale; rerun provisioning"})
    except DomainError:
        findings.append({"check": "setup projection", "ok": False, "detail": "no valid isolated assignment"})
    return {"business_id": business_id, "ok": all(item["ok"] for item in findings), "findings": findings}
