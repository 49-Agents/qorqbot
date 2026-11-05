"""Read-only proxy IP eligibility: canonical assignments plus an explicitly supplied audit snapshot.

The snapshot is evidence, never an assignment store. It is sanitized: IP-keyed
classifications only, with no persona names, source paths or credentials.
Unknown is not free. A persona assignment blocks allocation even if that
persona is empty or HN-only. Hacker News (HN) accounts alone do not block an IP
with neither a persona nor a business assignment.

Statuses
    available  no business/persona holds it, with fresh clean non-HN evidence
    assigned   another business's saved browser block (enabled or not) claims it
    occupied   the snapshot records a non-HN account/persona or reservation
    unknown    missing, stale, malformed or inconclusive evidence
"""

import argparse
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import sys

from qorqut_operations.contracts import DomainError
from .config import numeric_ipv4, reserved_ips

SCHEMA = 1
MAX_BYTES = 2 * 1024 * 1024
MAX_AGE = timedelta(days=7)
CLEAN = "no-known-non-HN-account"
OCCUPIED = "occupied/reserved"
PROFILE_CHECKS = {"no common non-HN login-cookie markers found": "clean", "not needed: recorded assignment": "not-needed",
                  "profile unavailable": "unavailable"}
ENTRY_KEYS = {"classification", "profile_check", "non_hn_platform_count", "persona_assigned", "exit_verified", "checked_at"}
TOP_KEYS = {"schema", "generated_at", "source", "ips"}


def _stamp(value):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise DomainError("validation_error", "Inventory timestamps must be ISO-8601 UTC") from None
    if moment.tzinfo is None:
        raise DomainError("validation_error", "Inventory timestamps must include a UTC offset")
    return moment.astimezone(timezone.utc)


def parse_snapshot(value):
    """Strict schema: unknown keys (persona names, paths) are refused, not silently carried."""
    if not isinstance(value, dict) or set(value) != TOP_KEYS or value["schema"] != SCHEMA:
        raise DomainError("validation_error", "Inventory snapshot must be a version-1 object with only " + ", ".join(sorted(TOP_KEYS)))
    _stamp(value["generated_at"])
    if not isinstance(value["source"], str) or len(value["source"]) > 200 or "/" in value["source"]:
        raise DomainError("validation_error", "Inventory source must be a short label, not a path")
    if not isinstance(value["ips"], dict) or len(value["ips"]) > 20000:
        raise DomainError("validation_error", "Inventory ips must be an object keyed by IPv4 address")
    for address, entry in value["ips"].items():
        numeric_ipv4(address, what="Inventory address")
        if (not isinstance(entry, dict) or not ENTRY_KEYS >= set(entry) >= {"classification", "checked_at"}
                or not isinstance(entry["classification"], str) or len(entry["classification"]) > 64):
            raise DomainError("validation_error", "Inventory entry for " + address + " has unsupported fields")
        _stamp(entry["checked_at"])
        if "exit_verified" in entry and type(entry["exit_verified"]) is not bool:
            raise DomainError("validation_error", "exit_verified must be a boolean")
        if "persona_assigned" in entry and type(entry["persona_assigned"]) is not bool:
            raise DomainError("validation_error", "persona_assigned must be a boolean")
        count = entry.get("non_hn_platform_count", 0)
        if type(count) is not int or not 0 <= count <= 1000:
            raise DomainError("validation_error", "non_hn_platform_count must be a small integer")
    return value


def load_snapshot(path):
    """An explicitly supplied private file; never discovered, never a link, never group/world readable."""
    path = Path(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > MAX_BYTES:
            raise DomainError("validation_error", "Inventory snapshot must be an ordinary file that others cannot write")
        return parse_snapshot(json.loads(path.read_text()))
    except (OSError, ValueError):
        raise DomainError("validation_error", "Inventory snapshot could not be read as JSON") from None


def snapshot_from_csv(path, *, generated_at, source="sanitized proxy audit"):
    """Sanitize an audit CSV, retaining persona occupancy but never persona names.

    An explicit empty ``persona`` cell attests no assignment. An absent column
    or missing cell is unknown, never inferred free from HN/non-HN accounts.
    """
    ips, generated = {}, _stamp(generated_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, newline="") as source_file:
        for row in csv.DictReader(source_file):
            address = numeric_ipv4(row["ip"], what="Audit address")
            if address in ips:
                raise DomainError("validation_error", "Audit contains a duplicate IP; reconcile its assignments first")
            platforms = [name for name in row.get("non_hn_platforms", "").split(";") if name.strip()]
            ips[address] = {"classification": row["classification"], "checked_at": generated,
                            "profile_check": PROFILE_CHECKS.get(row.get("profile_metadata_check", ""), "unavailable"),
                            "non_hn_platform_count": len(platforms),
                            "exit_verified": row.get("exit_ip_checked_from_dushman", "").startswith("yes")}
            if row.get("persona") is not None:
                ips[address]["persona_assigned"] = bool(row["persona"].strip())
    return parse_snapshot({"schema": SCHEMA, "generated_at": generated, "source": source, "ips": ips})


def assignments(connection):
    """Every IP any business's saved browser block claims, enabled or not."""
    found = {}
    for identity, slug, raw in connection.execute("SELECT id, slug, config_json FROM businesses"):
        try:
            block = json.loads(raw).get("browser") if isinstance(raw, str) else (raw or {}).get("browser")
        except (ValueError, AttributeError):
            continue
        for address in reserved_ips(block):
            found.setdefault(address, []).append({"business_id": identity, "slug": slug})
    return found


def classify(address, snapshot, held, *, business_id=None, now=None, max_age=MAX_AGE):
    """One IP's status with the evidence behind it. ``business_id`` is the business asking (it may keep its own IP)."""
    now = now or datetime.now(timezone.utc)
    others = [owner for owner in held.get(address, []) if owner["business_id"] != business_id]
    if others:
        return {"ip": address, "status": "assigned", "evidence": {"assigned_to": [o["slug"] for o in others]}}
    if snapshot is None:
        return {"ip": address, "status": "unknown", "evidence": {"reason": "no audit snapshot supplied"}}
    entry = snapshot["ips"].get(address)
    if entry is None:
        return {"ip": address, "status": "unknown", "evidence": {"reason": "not in the audit snapshot",
                                                                  "snapshot_generated_at": snapshot["generated_at"]}}
    evidence = {"classification": entry["classification"], "profile_check": entry.get("profile_check", "unavailable"),
                "persona_assigned": entry.get("persona_assigned"),
                "exit_verified": entry.get("exit_verified", False), "checked_at": entry["checked_at"]}
    if entry.get("persona_assigned") is True:
        return {"ip": address, "status": "occupied", "evidence": {**evidence, "reason": "assigned to a persona"}}
    if entry["classification"] == OCCUPIED or entry.get("non_hn_platform_count", 0) > 0:
        return {"ip": address, "status": "occupied", "evidence": evidence}
    if now - _stamp(entry["checked_at"]) > max_age or _stamp(entry["checked_at"]) - now > timedelta(minutes=5):
        return {"ip": address, "status": "unknown", "evidence": {**evidence, "reason": "evidence is stale or dated in the future"}}
    if entry.get("persona_assigned") is not False:
        return {"ip": address, "status": "unknown", "evidence": {**evidence, "reason": "persona assignment was not checked"}}
    if entry["classification"] != CLEAN or evidence["profile_check"] != "clean":
        return {"ip": address, "status": "unknown", "evidence": {**evidence, "reason": "the audit could not confirm a clean profile check"}}
    return {"ip": address, "status": "available", "evidence": evidence}


def report(database, snapshot, ips, *, business_id=None, now=None, max_age=MAX_AGE):
    with database.read() as uow:
        held = assignments(uow.connection)
    return {address: classify(numeric_ipv4(address, what="Address"), snapshot, held, business_id=business_id,
                              now=now, max_age=max_age) for address in ips}


def main(argv=None):
    from bazarlyk.composition.assembly import host_assembly, open_database
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_instance_arguments(parser)
    parser.add_argument("--snapshot", type=Path, help="explicit sanitized audit snapshot (JSON)")
    parser.add_argument("--from-csv", type=Path, help="sanitize an audit all-ips.csv into a snapshot on stdout instead")
    parser.add_argument("--generated-at", help="audit time (ISO-8601 UTC); required with --from-csv")
    parser.add_argument("--business-id", help="the asking project, which may keep an IP it already holds")
    parser.add_argument("ips", nargs="*")
    args = parser.parse_args(argv)
    try:
        if args.from_csv:
            if not args.generated_at:
                raise DomainError("validation_error", "--generated-at is required with --from-csv")
            print(json.dumps(snapshot_from_csv(args.from_csv, generated_at=args.generated_at), indent=1, sort_keys=True))
            return 0
        paths = instance_paths_from_arguments(args, required=())
        snapshot = load_snapshot(args.snapshot) if args.snapshot else None
        print(json.dumps(report(open_database(paths, host_assembly(ChangeFeed())), snapshot, args.ips, business_id=args.business_id),
                         indent=1, sort_keys=True))
        return 0
    except (DomainError, OSError, ValueError) as error:
        print("Inventory check failed: " + (error.message if isinstance(error, DomainError) else "unreadable input"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
