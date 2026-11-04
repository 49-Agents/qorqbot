"""Explicit fresh installation and strict, read-only opening of the host binding.

The private record holds initial provenance and native bindings. Current authored
instructions live only in the module tree and are expected to change.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import base64
from importlib import metadata, resources
import json
import os
from pathlib import Path
import re
import shutil

from qorqut_agentic import AgenticLayer
from qorqut_operations.contracts import DomainError

from bazarlyk.source_snapshot import unique_object, validate_export
from bazarlyk.private_files import (fsync_directory, read_private_file, require_private_directory,
                                    write_private_file)
from .inventory import inventory_sources
from .plans import apply_import_plan, build_import_plan, _validate_manifest
from .support import canonical, digest, relative
from .view import HostAgenticView

RECORD = "agentic-layer.json"
MAX_RECORD = 96 * 1024 * 1024
SOURCES = {"host": "qorqut-code", "roles": "qorqut-agentic", "email": "email-management",
           "content": "qorqut-content", "content-library": "content-library", "paper": "qorqut-paper", "fastmail": "qorqut-fastmail"}
CORE = {"email-management", "agent-terminal-control"}
OPTIONAL = {
    "telegram": {"sources": {"telegram"}, "packages": {"imported-telegram"}, "bindings": {("extension", "telegram")}},
    "whatsapp": {"sources": {"whatsapp"}, "packages": {"imported-whatsapp"}, "bindings": {("extension", "whatsapp")}},
    "content": {"sources": {"content", "content-library"}, "packages": {"imported-content"},
                "bindings": {("extension", "content"), ("plugin", "content-creation")}},
    "paper": {"sources": {"paper"}, "packages": {"imported-paper-metadata"}, "bindings": {("extension", "paper")}},
    "fastmail": {"sources": {"fastmail"}, "packages": {"imported-fastmail-metadata"}, "bindings": {("extension", "fastmail")}},
}
IDENTITY = {"name": "Qorqut", "email": "qorqut@localhost"}


def _failure(message):
    return DomainError("agentic_installation_invalid", message + "; an existing installation requires an explicit offline upgrade")


# The packaged native packages the host itself seeds into the module tree. Their files are authored content the
# installation owns; upgrades compare the recorded seed, the installed tree and the packaged bytes.
SEEDS = {
    "user-preferences": ("resources/defaults/user-preferences", ("agentic.yaml", "SKILL.md")),
    "agentic-management": ("resources/defaults/operations/agentic-management", ("agentic.yaml", "bootstrap.md", "skills/manage/SKILL.md")),
    "overseer": ("resources/defaults/overseer/overseer", ("agentic.yaml", "instructions/extract.md", "instructions/review.md",
                                        "instructions/apply.md", "workflows/extract.yaml", "workflows/review.yaml",
                                        "workflows/apply.yaml")),
}
OVERSEER_REFS = ("overseer/extract-instructions", "overseer/review-instructions", "overseer/apply-instructions",
                 "overseer/record-direct-instruction", "overseer/extract", "overseer/review", "overseer/apply")


def seed_files(package="agentic-management"):
    """The exact packaged bytes of one host-seeded native package, bounded and validated."""
    resource, required = SEEDS[package]
    root = resources.files("qorqut_agentic").joinpath(resource)
    result = {}
    def walk(node, prefix=""):
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            relative = prefix + child.name
            if child.is_dir():
                walk(child, relative + "/")
            elif child.is_file():
                body = child.read_bytes()
                if len(body) > 1024 * 1024:
                    raise _failure(f"Oversized packaged {package} resource")
                result[relative] = body
            else:
                raise _failure(f"Invalid packaged {package} resource")
    walk(root)
    if "agentic.yaml" not in result or not set(required) <= result.keys():
        raise _failure(f"Missing packaged {package} manifest or required entries")
    return result


def _seed_files():
    return seed_files("agentic-management")


def _seed_provenance(package, seed):
    resource, _ = SEEDS[package]
    return {"distribution": "qorqut-agentic", "version": metadata.version("qorqut-agentic"),
            "resource": "qorqut_agentic/" + resource,
            "files": {path: digest(body) for path, body in sorted(seed.items())}}


def prepare_installation(export_root, selected_extensions):
    """Read-only preflight from the verified core export and selected connector wheels."""
    try:
        manifest, manifest_digest = validate_export(export_root)
        components = {c["name"]: c for c in manifest["components"]}
        if "qorqut-agentic" not in components:
            raise _failure("Missing qorqut-agentic source component")
        roots = {key: str(Path(export_root) / components[name]["destination"] / ("src/qorqut_agentic/resources/defaults" if key == "roles" else ""))
                 for key, name in SOURCES.items() if name in components}
        selected = sorted(set(selected_extensions))
        installed = []
        for connector in ("telegram", "whatsapp", "fastmail"):
            if connector in selected:
                if connector not in roots and connector in {"telegram", "whatsapp"} and "qorqut-code" in components:
                    roots[connector] = str(Path(export_root) / components["qorqut-code"]["destination"] / "extensions" / connector)
                if connector not in roots or not Path(roots[connector]).is_dir():
                    from .installed_sources import installed_source
                    roots[connector] = installed_source(connector)["root"]
                    installed.append(connector)
        inventory = inventory_sources(roots, sorted(CORE | (set(selected) & OPTIONAL.keys())), export_root=export_root, installed_sources=installed)
        if inventory["coverage"]["complete"] is not True:
            raise _failure("Incomplete agentic source inventory: " + json.dumps(inventory["diagnostics"]))
        # Validate the host-owned record contract before any target write.
        HostAgenticView(None, inventory)
        _verify_inventory(inventory, selected, manifest_digest)
        plan = build_import_plan(inventory)
        from .support import Source
        source = Source("roles", roots["roles"], "roles", True, [], export_component=components["qorqut-agentic"], manifest_digest=manifest_digest)
        def bundle(path):
            return {item.removeprefix(path + "/"): source.read(item) for item in source.tree(path)}
        seed, overseer, preferences = bundle("operations/agentic-management"), bundle("overseer/overseer"), bundle("user-preferences")
        for package, content in (("agentic-management", seed), ("overseer", overseer)):
            if not set(SEEDS[package][1]) <= content.keys():
                raise _failure("Missing required " + package + " defaults")
        if not {"agentic.yaml", "SKILL.md"} <= preferences.keys():
            raise _failure("Missing required user preferences defaults")
        from bazarlyk import native_paths
        git = (native_paths.executable("git") if native_paths.root() else shutil.which("git", path=os.defpath))
        if git is None:
            raise _failure("Git is required for agentic initialization")
        return {"inventory": inventory, "plan": plan, "seed": seed, "overseer_seed": overseer, "git": git,
                "preferences_seed": preferences, "selected_extensions": selected, "source_manifest_sha256": manifest_digest,
                "export_root": str(Path(export_root))}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _failure(str(exc)) from exc


def initialize_installation(paths, prepared):
    """Initialize once, author through the public API, then atomically seal binding."""
    # Re-read the verified export so a caller cannot substitute a generic import
    # plan or alter the protected bytes between preflight and installation.
    verified = prepare_installation(prepared["export_root"], prepared["selected_extensions"])
    if verified != prepared:
        raise _failure("Prepared sources or installation plan changed after preflight")
    prepared = verified
    # This writer is private to fresh initialization and accepts only the preflight's
    # verified create-only plan. Public import/authoring APIs retain their area policy.
    for target in (paths.agentic_root, paths.agentic_runtime):
        if any(part.is_symlink() for part in (target, *target.parents)):
            raise _failure("Fresh agentic paths cannot traverse symlinks")
        if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
            raise _failure("Fresh agentic target must be absent or empty")
    plan = prepared["plan"]
    if (plan.get("plan_digest") != digest({k: v for k, v in plan.items() if k != "plan_digest"})
            or plan.get("conflicts") or not plan.get("coverage", {}).get("complete")
            or any(change.get("op") != "create" for change in plan["changes"])):
        raise _failure("Fresh import plan is invalid")
    files = {change["path"]: base64.b64decode(change["content_base64"], validate=True) for change in plan["changes"]}
    for prefix, seed in (("operations/agentic-management", prepared["seed"]),
                         ("overseer/overseer", prepared["overseer_seed"]),
                         ("user-preferences", prepared["preferences_seed"])):
        for path, body in seed.items():
            target = prefix + "/" + relative(path)
            if target in files:
                raise _failure("Fresh defaults conflict with imported content")
            files[target] = body
    if {path: digest(body) for path, body in files.items() if path in plan["desired_files"]} != plan["desired_files"]:
        raise _failure("Fresh import bytes do not match the plan")
    layer = AgenticLayer.initialize(paths.agentic_root, paths.agentic_runtime, git_executable=prepared["git"],
                                    git_user=IDENTITY, attribution_source="host_installer")
    attribution = {"actor_id": "system:installer", "actor_kind": "system", "reason": "Initialize agentic procedures from verified prepared sources",
                   "source_refs": ["source-manifest:sha256:" + prepared["source_manifest_sha256"]]}
    for path, body in sorted(files.items()):
        if layer._ctx.tree.kind(path) != "missing":
            raise _failure("Fresh installation destination already exists")
        layer._ctx.tree.write_atomic(path, body)
    layer.changes.scan(attribution)
    report = layer.catalog.validate()
    if not report["valid"] or layer.recovery_required:
        raise _failure("Initialized agentic catalog is invalid")
    for ref in ("agentic-management/bootstrap", "agentic-management/manage", "user-preferences/preferences", *OVERSEER_REFS):
        layer.catalog.read_entry(ref)
    git = layer.history.git
    git.run(["add", "-A", "--", *sorted(files)])
    git.run(["commit", "--no-verify", "--no-gpg-sign", "-q", "-m", "Initialize imported procedures and agentic management guidance"])
    commit = layer.history._head()
    layer.changes.link_commit(commit, {path: digest(body) for path, body in files.items()})
    manifest = dict(schema_version=1, source_digest=plan["source_digest"], plan_digest=plan["plan_digest"],
                    previous_import_digest=plan["previous_import_digest"], existing_digest=plan["existing_digest"],
                    files=deepcopy(plan["desired_files"]), bindings=deepcopy(plan["bindings"]))
    manifest["manifest_digest"] = digest(manifest)
    imported = {"import_manifest": manifest}
    seed, overseer = prepared["seed"], prepared["overseer_seed"]
    inventory = prepared["inventory"]
    record = {"version": 2, "instance_id": layer.instance_id, "root": str(paths.agentic_root), "state": str(paths.agentic_runtime),
              "inventory": inventory, "inventory_sha256": digest(inventory), "import_manifest": imported["import_manifest"],
              "source_manifest_sha256": prepared["source_manifest_sha256"], "selected_extensions": prepared["selected_extensions"],
              "initial_commit": commit, "management_seed": _seed_provenance("agentic-management", seed),
              "overseer_seed": _seed_provenance("overseer", overseer),
              "preferences_seed": _seed_provenance("user-preferences", prepared["preferences_seed"])}
    raw = canonical(record) + b"\n"
    if len(raw) > MAX_RECORD:
        raise _failure("Agentic binding exceeds its size bound")
    directory = require_private_directory(paths.instance_root / "config", create=True)
    # The existing private writer supplies ownership, exclusive create and fsync.
    # Publish only its complete bytes under the final name; never replace a binding.
    temp = write_private_file(directory, ".agentic-layer.pending", raw)
    os.link(temp, directory / RECORD, follow_symlinks=False)
    os.unlink(temp)
    fsync_directory(directory)
    return {"path": str(directory / RECORD), "sha256": digest(raw), "instance_id": layer.instance_id, "initial_commit": commit}


def _verify_inventory(inventory, selected, manifest_digest):
    required = {"host", "roles", "email"}
    for owner in set(selected) & OPTIONAL.keys():
        required.update(OPTIONAL[owner]["sources"])
    sources = {s["id"] for s in inventory["sources"]}
    if not required <= sources or inventory["coverage"]["asset_count"] != len(inventory["assets"]) or inventory["coverage"]["package_count"] != len(inventory["packages"]):
        raise ValueError("Incomplete inventory source or coverage records")
    for source in inventory["sources"]:
        explicit_custom = source["id"].startswith("custom-") or source["id"] == "email-guidance"
        if explicit_custom:
            # The offline importer deliberately supports explicit unversioned
            # custom snapshots; ordinary installed sources still require exports.
            commit = source.get("commit")
            if (commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit))) or (source.get("clean") is not None and source.get("clean") is not True):
                raise ValueError("Invalid explicit custom-source provenance")
        elif source.get("provenance_kind") == "installed_distribution":
            if (source["id"] not in {"telegram", "whatsapp", "fastmail"} or source.get("distribution") != "qorqut-" + source["id"]
                    or source.get("commit") is not None or source.get("clean") is not None
                    or not isinstance(source.get("version"), str) or not source["version"]
                    or not re.fullmatch(r"[0-9a-f]{64}", source.get("distribution_record_sha256", ""))
                    or not isinstance(source.get("distribution_files"), dict)
                    or any(source["distribution_files"].get(path) != sha for path, sha in source["files"].items())):
                raise ValueError("Invalid installed-distribution provenance")
        else:
            if source.get("provenance_kind") != "committed_export" or source.get("clean", False) is not None:
                raise ValueError("Installed source lacks committed-export provenance")
            for key, size in (("commit", 40), ("tree", 40), ("snapshot_sha256", 64), ("source_manifest_sha256", 64)):
                if not isinstance(source.get(key), str) or not re.fullmatch(r"[0-9a-f]{" + str(size) + "}", source[key]):
                    raise ValueError("Malformed exported-source provenance")
            if source["source_manifest_sha256"] != manifest_digest:
                raise ValueError("Source manifest digest mismatch")
        if source["snapshot_digest"] != digest(source["files"]):
            raise ValueError("Source provenance digest mismatch")
        for path, sha in source["files"].items():
            relative(path)
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("Invalid used-file provenance")
    packages = {p["id"]: p for p in inventory["packages"]}
    expected = {"imported-host-builtins", "imported-roles", "imported-email-management", "imported-terminal-control", "runtime-instructions"}
    if not expected <= packages.keys() or any(not packages[p]["native"]["enabled"] for p in expected):
        raise ValueError("Missing required active core packages")
    roles = {b["native_id"] for b in inventory["bindings"] if b["kind"] == "role" and b["enabled"]}
    if not {"ordubashy", "global-worker", "learning-applier"} <= roles:
        raise ValueError("Missing required role bindings")


def _seed_version():
    """The distribution that supplies both packaged seeds in this build.

    The management and Overseer seeds are packaged by the same installed ``qorqut-agentic`` distribution, so the sealed
    binding records the version that actually supplied its files instead of a fixed release string. A binding written by
    an earlier distribution keeps its own recorded version until the explicit offline upgrade replaces it; that upgrade
    records the pre-upgrade provenance verbatim as its ``previous`` side rather than relabelling it.
    """
    return metadata.version("qorqut-agentic")


def _verify_seed(seed, package, required, *, exact_version):
    if (not isinstance(seed, dict) or set(seed) != {"distribution", "version", "resource", "files"}
            or seed["distribution"] != "qorqut-agentic" or not isinstance(seed['version'], str)
            or not seed['version'].strip() or (exact_version is not None and seed["version"] != exact_version)
            or seed["resource"] != "qorqut_agentic/" + SEEDS[package][0] or not isinstance(seed["files"], dict)
            or not set(required) <= seed["files"].keys()
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in seed["files"].values())):
        raise ValueError(f"Malformed {package} seed provenance")
    for path in seed["files"]:
        relative(path)


def current_seed_files(paths, package):
    """The installed seed tree for one seeded package, read without following links; missing files are absent."""
    destinations = {"agentic-management": "operations/agentic-management",
                    "overseer": "overseer/overseer", "user-preferences": "user-preferences"}
    root = paths.agentic_root / destinations[package]
    result = {}
    if not root.is_dir() or any(part.is_symlink() for part in (root, *root.parents)):
        return result
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative_path = path.relative_to(root).as_posix()
        relative(relative_path)
        result[relative_path] = path.read_bytes()
    return result


def _active_inventory(layer, inventory, selected):
    """Derive activation from trusted current owners without changing provenance.

    Template classification and native bindings come from the verified original,
    never from editable manifests. Explicit custom-source choices stay intact.
    """
    packages = {p["id"]: p for p in inventory["packages"]}
    sources = {s["id"]: s for s in inventory["sources"]}
    bindings = {(b["kind"], b["native_id"]): b for b in inventory["bindings"]}
    for owner in selected & OPTIONAL.keys():
        expected = OPTIONAL[owner]
        if (not expected["sources"] <= sources.keys() or not expected["packages"] <= packages.keys()
                or not expected["bindings"] <= bindings.keys()):
            raise ValueError("Selected " + owner + " procedures were not imported; an offline import/update is required")
        if (any(sources[key]["owner"] != owner for key in expected["sources"])
                or any(packages[key]["native"]["owner"] != owner for key in expected["packages"])
                or any(bindings[key]["owner"] != owner or bindings[key]["template"]
                       or bindings[key]["source_id"] not in expected["sources"]
                       or bindings[key]["package_id"] not in expected["packages"] for key in expected["bindings"])):
            raise ValueError("Invalid imported binding for selected " + owner)
        report = layer.catalog.validate(package_ids=sorted(expected["packages"]))
        if not report["valid"] or not expected["packages"] <= {p["id"] for p in report["packages"]}:
            raise ValueError("Selected " + owner + " authored packages are missing or invalid; an offline update is required")
    active = deepcopy(inventory)
    source_owners = {source: owner for owner, spec in OPTIONAL.items() for source in spec["sources"]}
    template_packages = {a["package_id"] for a in inventory["assets"] if a["template"]}
    template_packages.update(b["package_id"] for b in inventory["bindings"] if b["template"])
    optional_packages = {a["package_id"] for a in inventory["assets"] if a["source_id"] in source_owners}
    for source in active["sources"]:
        if source["id"] in source_owners:
            source["enabled"] = source_owners[source["id"]] in selected
    for package in active["packages"]:
        if package["id"] not in optional_packages:
            continue
        enabled = package["native"]["owner"] in selected and package["id"] not in template_packages
        package["native"]["enabled"] = enabled
        for entry in package["entries"]:
            entry["native"]["enabled"] = enabled and not entry["native"].get("template") and entry["kind"] != "template"
    for asset in active["assets"]:
        if asset["source_id"] in source_owners:
            asset["enabled"] = source_owners[asset["source_id"]] in selected and not asset["template"] and asset["package_id"] not in template_packages
    for binding in active["bindings"]:
        if binding["source_id"] in source_owners:
            binding["enabled"] = source_owners[binding["source_id"]] in selected and not binding["template"] and binding["package_id"] not in template_packages
    active["source_digest"] = digest(active["sources"])
    return active


def _validate_binding(paths, record, *, _verified_candidate=None, _historical=False):
    """The binding record's complete self-consistency, without opening the module or writing anything.

    The offline upgrade runs this on the record it is about to publish, so an incomplete migration is refused before any
    binding replaces the installed one; ``open_installation`` then applies the same checks to the bytes on disk.
    """
    keys = {"version", "instance_id", "root", "state", "inventory", "inventory_sha256", "import_manifest",
            "source_manifest_sha256", "selected_extensions", "initial_commit", "management_seed", "overseer_seed", "preferences_seed"}
    if (not isinstance(record, dict) or type(record["version"]) is not int or record["version"] != 2
            or set(record) not in (keys, keys | {"layout_upgrade"})):
        raise ValueError("Unsupported host<->module binding or legacy layout; an explicit offline upgrade is required")
    if record["root"] != str(paths.agentic_root) or record["state"] != str(paths.agentic_runtime):
        raise ValueError("Agentic binding paths differ from InstancePaths")
    if (not isinstance(record["selected_extensions"], list)
            or not all(isinstance(value, str) and value for value in record["selected_extensions"])
            or record["selected_extensions"] != sorted(set(record["selected_extensions"]))):
        raise ValueError("Malformed initial trusted extension selection")
    if not isinstance(record["instance_id"], str) or not re.fullmatch(r"inst_[0-9a-f]{32}", record["instance_id"]):
        raise ValueError("Malformed module identity")
    for key in ("inventory_sha256", "source_manifest_sha256", "initial_commit"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[0-9a-f]{" + ("40" if key == "initial_commit" else "64") + "}", record[key]):
            raise ValueError("Malformed binding digest or commit")
    if record["inventory_sha256"] != digest(record["inventory"]):
        raise ValueError("Inventory digest mismatch")
    _verify_inventory(record["inventory"], record["selected_extensions"], record["source_manifest_sha256"])
    _validate_manifest(record["import_manifest"])
    if record["import_manifest"]["bindings"] != record["inventory"]["bindings"]:
        raise ValueError("Import manifest does not preserve the complete inventory")
    if record["import_manifest"]["source_digest"] != record["inventory"]["source_digest"]:
        raise ValueError("Import manifest differs from the bound inventory")
    if "layout_upgrade" in record:
        from .layout_binding import build_candidate
        metadata = record["layout_upgrade"]
        if not isinstance(metadata, dict) or set(metadata) != {"format_version", "plan_digest", "prior_binding_sha256", "intent_sha256", "git_receipt_sha256", "resulting_commit", "preference_origin"} or type(metadata["format_version"]) is not int or metadata["format_version"] != 1:
            raise ValueError("Malformed layout upgrade metadata")
        from .layout_intent import _read_intent
        from .upgrade import history_directory
        pd = metadata["plan_digest"]
        if not isinstance(pd, str) or not re.fullmatch(r"[0-9a-f]{64}", pd):
            raise ValueError("Malformed layout plan digest")
        if _verified_candidate is None:
            plan = _read_intent(history_directory(paths) / ("layout-" + pd + ".json"))["plan"]
            expected, _, _ = build_candidate(paths, plan)
        else:
            expected = _verified_candidate
        if record != expected:
            raise ValueError("Migrated binding differs from retained evidence")
        # Candidate derivation already checked the same inventory and built its
        # complete import plan. Equality above proves those exact files here.
        return record
    expected_import = build_import_plan(record["inventory"])
    if record["import_manifest"]["files"] != expected_import["desired_files"]:
        raise ValueError("Import manifest does not preserve the complete inventory")
    _verify_seed(record["management_seed"], "agentic-management", ("agentic.yaml", "bootstrap.md", "skills/manage/SKILL.md"),
                 exact_version=None if _historical else _seed_version())
    _verify_seed(record["overseer_seed"], "overseer", ("agentic.yaml", "instructions/extract.md"),
                 exact_version=None if _historical else _seed_version())
    _verify_seed(record["preferences_seed"], "user-preferences", ("agentic.yaml", "SKILL.md"),
                 exact_version=None if _historical else _seed_version())
    return record


@contextmanager
def _validated_installed_binding(paths, *, timeout, offline_read_only=False):
    """Verify current binding and activation once under the shared authoring lock."""
    from qorqut_agentic.store import Store
    from .layout_intent import retained_intents, _retained_reads
    from .layout_activation import incomplete, validate_completed
    with Store(paths.agentic_runtime).lock(exclusive=False, timeout=timeout), _retained_reads():
        record = json.loads(read_private_file(paths.instance_root / "config" / RECORD, max_bytes=MAX_RECORD), object_pairs_hook=unique_object)
        from . import managed_journal
        managed_evidence = managed_journal.discover(paths)
        if any('journal' not in pair or managed_journal.read(pair['journal'])['phase'] != 'complete'
               for pair in managed_evidence.values()):
            raise ValueError('Unfinished managed update; explicit offline resume is required')
        intents = retained_intents(paths)
        verified = {}
        if incomplete(paths, intents, _verified=verified, offline_read_only=offline_read_only):
            raise ValueError("Unfinished layout upgrade; explicit offline resume is required")
        if 'managed_upgrade' in record or managed_evidence:
            from .managed_ancestry import verify
            verify(paths, record, read_private_file(paths.instance_root / 'config' / RECORD, max_bytes=MAX_RECORD),
                   layout_intents=intents, layout_verified=verified, offline_read_only=offline_read_only)
        else:
            candidate = validate_completed(paths, record, _intents=intents, _verified=verified,
                                           offline_read_only=offline_read_only)
            _validate_binding(paths, record, _verified_candidate=candidate)
        yield record


def open_installation(paths, selected_extensions):
    """Open without writes; derive current activation from the trusted selection."""
    try:
        with _validated_installed_binding(paths, timeout=10) as record:
            return _open_validated_record(paths, record, selected_extensions)
    except Exception as exc:
        raise _failure("Cannot open installed agentic layer: " + str(exc)) from exc


def _open_record(paths, record, selected_extensions, *, _verified_candidate=None):
    """Validate and open one binding record without writing; the shared body of an installed open.

    ``open_installation`` applies it to the private bytes; the offline upgrade applies it to the complete record it is
    about to publish, so it never seals a binding that could not be opened.
    """
    _validate_binding(paths, record, _verified_candidate=_verified_candidate)
    return _open_validated_record(paths, record, selected_extensions)


def _open_validated_record(paths, record, selected_extensions):
    layer = AgenticLayer.open(paths.agentic_root, paths.agentic_runtime, attribution_source="host_authenticated")
    return _validate_open_layer(paths, record, selected_extensions, layer)


def _validate_open_layer(paths, record, selected_extensions, layer):
    git_directory = paths.agentic_root / ".git"
    if git_directory.is_symlink() or not git_directory.is_dir():
        raise ValueError("The initialized agentic Git repository is missing or not a local directory")
    if layer.instance_id != record["instance_id"]:
        raise ValueError("Wrong agentic module instance")
    if not layer.history.status()["head"]:
        raise ValueError("The initialized agentic Git repository has no usable HEAD commit")
    if layer.recovery_required or not layer.catalog.validate()["valid"]:
        raise ValueError("Agentic state requires recovery or catalog correction")
    for ref in ("agentic-management/bootstrap", "agentic-management/manage", "user-preferences/preferences", *OVERSEER_REFS):
        layer.catalog.read_entry(ref)
    HostAgenticView(layer, record["inventory"])
    active = _active_inventory(layer, record["inventory"], set(selected_extensions))
    return layer, HostAgenticView(layer, active)
