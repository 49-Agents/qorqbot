"""Offline adoption of a release's role and plugin packages by an existing installation.

A fresh installation publishes the shipped roles and plugins and pins them in the first agent profiles
(``bootstrap/initialize.py``). An existing installation keeps the packages and pins it holds: the instruction update replaces
the authored text of roles, skills and plugins, while the catalog packages in the database (immutable, digest-pinned) and the
pins in the agent profiles change only through owner operations (``roles_publish``/``plugins_publish`` and ``agents_update``).
A pin that stays behind the release has these consequences: the role and plugin text agents read still comes from the authored
layer (role brief, skills and tool wording are read from it by package id, not from the pinned copy), but the pinned manifest
decides which tools and skills exist, the role version shown in the agent's identity is the pinned one, and a plugin the
release gives Ordubashy's profile is absent (the agent-control tools).

``inspect`` and ``apply`` use the same arguments as the service start command (``--workspace``, ``--instance-root``, ``--component``,
``--extensions-config``), run with the service stopped and after ``qorqut.migration.upgrade apply``. Both take the instance
ownership, so a running service refuses them. ``apply`` does exactly what the owner would do by hand, through the same operations:

1. publish every package of the release's ``package-sources.json`` that the installation does not hold yet (a published version
   whose bytes differ is reported as a conflict and left alone);
2. move each active agent's role pin and each plugin pin to the release's version when that is newer (a plugin's config is kept);
3. give Ordubashy the plugins in ``ENSURED`` (the agent-control tools) when his profile has none, with an empty config.

Every step is idempotent: a second run changes nothing, and an installation that is already current is not touched. Pins only
move forward. One ``agents_update`` is made per agent that needs a change, and nothing is written to the instruction history.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qorqut_operations.contracts import DomainError, InvocationOptions

from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments

# Plugin id -> the role whose profiles must hold it. A fresh installation gives these to Ordubashy at seed
# (``bootstrap/initialize.py: ORDUBASHY_PLUGINS``); a profile without them is given them here.
ENSURED = {"agent-terminal-control": "ordubashy"}


def _key(version):
    try:
        return tuple(int(part) for part in str(version).split("."))
    except (TypeError, ValueError):
        return None


def _newer(version, than):
    new, old = _key(version), _key(than)
    return new is not None and old is not None and new > old


def profile_changes(profile, refs):
    """``(changes, notes)`` for one stored profile: the ``agents_update`` changes that bring its pins to ``refs`` (the release's
    package references by ``(kind, id)``), or ``({}, [])`` when it is already current. Pins only move forward."""
    changes, notes = {}, []
    role = profile["role"]
    target = refs.get(("role", role["id"]))
    if target is not None and _newer(target["version"], role["version"]):
        changes["role"] = target
        notes.append(f"role {role['id']} {role['version']} -> {target['version']}")
    plugins, moved = [], False
    for grant in profile["plugins"]:
        target = refs.get(("plugin", grant["id"]))
        if target is not None and _newer(target["version"], grant["version"]):
            plugins.append({**target, "config": grant["config"]})
            notes.append(f"plugin {grant['id']} {grant['version']} -> {target['version']}")
            moved = True
        else:
            plugins.append(grant)
    for plugin_id, role_id in ENSURED.items():
        target = refs.get(("plugin", plugin_id))
        if target is not None and role["id"] == role_id and all(grant["id"] != plugin_id for grant in plugins):
            plugins.append({**target, "config": {}})
            notes.append(f"plugin {plugin_id} added at {target['version']}")
            moved = True
    if moved:
        changes["plugins"] = plugins
    return changes, notes


def plan(runtime, workspace, paths):
    """Read-only: what ``apply`` would do. Returns ``{"packages": [...], "profiles": [...], "conflicts": [...]}``."""
    from qorqut.catalog.packages import load_package
    from qorqut.catalog.sources import load_sources

    packages, conflicts, refs = [], [], {}
    with runtime.database.read() as uow:
        for source_id, (kind, root, manifest) in sorted(load_sources(workspace, paths=paths).items()):
            package = load_package(root, manifest, kind, runtime.registry)
            ref = package.ref
            row = uow.connection.execute("SELECT digest FROM artifacts WHERE kind=? AND artifact_id=? AND version=?",
                                         (kind, ref["id"], ref["version"])).fetchone()
            if row is not None and row["digest"] != ref["digest"]:
                conflicts.append({"source": source_id, "kind": kind, "version": ref["version"],
                                  "reason": "A different package is already published under this version"})
                continue
            refs[(kind, ref["id"])] = ref
            packages.append({"source": source_id, "kind": kind, "id": ref["id"], "version": ref["version"],
                             "action": "present" if row is not None else "publish"})
        profiles = []
        for row in uow.connection.execute("SELECT agent_id, name, revision, profile_json FROM agent_profiles "
                                          "WHERE status='active' ORDER BY name"):
            changes, notes = profile_changes(json.loads(row["profile_json"]), refs)
            if changes:
                profiles.append({"agent_id": row["agent_id"], "name": row["name"], "revision": row["revision"],
                                 "changes": changes, "summary": notes})
    return {"packages": packages, "profiles": profiles, "conflicts": conflicts}


def apply(runtime, owner_context, plan_):
    """Publish, then move the pins, through the owner's own operations. Returns what was done."""
    done = {"published": [], "profiles": []}
    try:
        _apply(runtime, owner_context, plan_, done)
    except ValueError as error:
        # Composing a profile against instruction text that does not match this release's code (the instruction update has not
        # been applied yet) fails with a plain ValueError; nothing has been written by that profile.
        raise DomainError("instructions_out_of_date", "The installed instructions do not match this release; run the instruction "
                          f"update before this step (what was done so far is kept; run it again afterwards): {error}") from error
    return done


def _apply(runtime, owner_context, plan_, done):
    for package in plan_["packages"]:
        if package["action"] != "publish":
            continue
        operation = "roles_publish" if package["kind"] == "role" else "plugins_publish"
        result = runtime.application.invoke(owner_context, operation, {"source_id": package["source"]},
                                            InvocationOptions(idempotency_key=f"adopt-{package['source']}-{package['version']}"))
        if result.status != "ok":
            raise DomainError("adoption_failed", f"Publishing {package['source']} {package['version']} was refused: {result.error}")
        done["published"].append(f"{package['kind']} {package['id']}@{package['version']}")
    for item in plan_["profiles"]:
        result = runtime.application.invoke(owner_context, "agents_update", {"agent_id": item["agent_id"], "changes": item["changes"]},
                                            InvocationOptions(idempotency_key=f"adopt-{item['agent_id']}-r{item['revision']}",
                                                              expected_revision=item["revision"]))
        if result.status != "ok":
            raise DomainError("adoption_failed", f"Updating the pins of {item['name']} was refused: {result.error}")
        done["profiles"].append({"agent_id": item["agent_id"], "name": item["name"], "summary": item["summary"]})


def run(options):
    from qorqut.bootstrap.initialize import owner_context
    from qorqut.composition.assembly import host_assembly, open_database
    from qorqut.extensions import installed_selection
    from qorqut.installation import extensions_configuration
    from qorqut.service.changes import ChangeFeed
    from qorqut.service.runtime import build_runtime

    paths = instance_paths_from_arguments(options, required=("app", "dashboard"))
    selection = (installed_selection() if options.extensions_config is None
                 else installed_selection(extensions_configuration(options.extensions_config)))
    database = open_database(paths, host_assembly(ChangeFeed(), selection))
    runtime = None
    try:
        runtime = build_runtime(options.workspace, paths=paths, database=database, extensions=selection)
        with runtime.owned_preparation():
            with database.read() as uow:
                owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()[0]
            wanted = plan(runtime, options.workspace, paths)
            result = {"plan": wanted}
            if options.command == "apply":
                result["done"] = apply(runtime, owner_context(owner), wanted)
        return result
    finally:
        if runtime is not None:
            runtime.domains.shared_context.close()
        database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["inspect", "apply"])
    parser.add_argument("--workspace", type=Path, required=True, help="The release folder the service is started with")
    add_instance_arguments(parser)
    parser.add_argument("--extensions-config", type=Path)
    options = parser.parse_args(argv)
    try:
        result = run(options)
        response, status = {"ok": True, **result}, 0
        if result["plan"]["conflicts"]:
            status = 1
    except DomainError as error:
        response, status = {"ok": False, "error": error.to_dict()}, 1
    sys.stdout.write(json.dumps(response, indent=1, default=str) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
