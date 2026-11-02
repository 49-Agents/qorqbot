"""Versioned release manifest and frozen-plan handling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

from .errors import ReleaseError, UnsupportedAdapter
from .io import digest

try:
    import yaml
except ImportError:  # pragma: no cover - the package declares PyYAML
    yaml = None

SCHEMA_VERSION = 1
HISTORY_REFS = {"qorqut-tables", "bazarlyk-history"}
BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
COMPONENT_CATEGORIES = {"core", "dashboard", "role", "plugin", "engine", "documentation", "outreach", "library"}
ADAPTERS = {
    "core": "qorqut-dashboard",
    "dashboard": "qorqut-dashboard",
    "role": "immutable-package",
    "plugin": "immutable-package",
    "engine": "independent-engine",
    "documentation": "documentation",
    "library": "source-library",
    "outreach": "independent-engine",
}
KNOWN_ADAPTERS = frozenset(ADAPTERS.values())


def load_data(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text()
        value = yaml.safe_load(raw) if path.suffix in {".yaml", ".yml"} and yaml else json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"manifest must be an object: {path}")
    return value


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(f"git query failed for {repo}: {exc.stdout.strip()[-400:]}") from exc


def _require_branch(value: str, *, field: str, allow_target: bool = False) -> None:
    if not value or not BRANCH.fullmatch(value) or value.startswith("-"):
        raise ReleaseError(f"invalid {field}: {value!r}")
    leaf = value.removeprefix("refs/heads/")
    if (allow_target and leaf in HISTORY_REFS) or (not allow_target and (leaf in HISTORY_REFS or leaf in {"main", "master"})):
        raise ReleaseError(f"{field} names a protected/non-task ref: {value}")


def _validate_direct_argv(value: Any, *, field: str) -> None:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise ReleaseError(f"{field} must be a non-empty direct argv list")
    if any(item in {";", "&&", "||", "|", ">", "<"} for item in value):
        raise ReleaseError(f"{field} must not contain shell syntax")


_CREDENTIAL_OPTION = re.compile(
    r"^(?:--?|/)[A-Za-z0-9_-]*(?:token|secret|password|passwd|api[-_]?key|authorization|cookie|credential|private[-_]?key)[A-Za-z0-9_-]*(?:=(.*))?$",
    re.I,
)
_CREDENTIAL_REFERENCE_OPTION = re.compile(r"(?:^|[-_])(?:file|path|env|fd|descriptor)$", re.I)
_CREDENTIAL_HEADER = re.compile(
    r"(?i)(?:^|[\s,])(?:authorization|proxy-authorization|cookie):\s*\S+"
)
_CREDENTIAL_URI = re.compile(r"(?i)^[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^@\s]+@")


def _validate_no_literal_credentials(argv: list[str], *, field: str) -> None:
    for index, item in enumerate(argv):
        option = _CREDENTIAL_OPTION.fullmatch(item)
        if option:
            option_name = item.split("=", 1)[0]
            if _CREDENTIAL_REFERENCE_OPTION.search(option_name):
                continue
            raise ReleaseError(
                f"{field} contains a literal credential option {option_name}; use an explicit *-file, *-env or *-fd reference"
            )
        if _CREDENTIAL_HEADER.search(item):
            raise ReleaseError(f"{field} contains a literal authorization header; use a credential-file reference")
        if _CREDENTIAL_URI.search(item):
            raise ReleaseError(f"{field} contains credentials embedded in a URI; use a credential-file reference")
        if re.search(
            r"(?i)^(?:token|secret|password|passwd|api[-_]?key|authorization|cookie|credential)\s*=\s*.+$",
            item,
        ):
            raise ReleaseError(f"{field} contains a literal credential assignment")


def _validate_command_value(value: Any, *, field: str, allow_string: bool = False) -> None:
    if isinstance(value, list):
        _validate_direct_argv(value, field=field)
        _validate_no_literal_credentials(value, field=field)
        return
    if allow_string and isinstance(value, str):
        try:
            argv = shlex.split(value)
        except ValueError as exc:
            raise ReleaseError(f"{field} is not a valid command") from exc
        _validate_direct_argv(argv, field=field)
        _validate_no_literal_credentials(argv, field=field)
        return
    raise ReleaseError(f"{field} must be a direct argv list")


def _validate_declared_commands(value: Any, *, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ReleaseError(f"{field} must be a list of commands")
    for index, raw in enumerate(value):
        command = raw.get("command") if isinstance(raw, dict) else raw
        _validate_command_value(command, field=f"{field}[{index}]", allow_string=True)


def _validate_command_secrets(value: dict[str, Any]) -> None:
    installation = value.get("installation", {})
    if isinstance(installation, dict):
        for key in ("launcher", "stopper", "quiescence_command", "runtime_install_command", "runtime_validate_command"):
            if key in installation:
                _validate_command_value(installation[key], field=f"installation.{key}")
    checks = value.get("checks", [])
    if isinstance(checks, list):
        for index, item in enumerate(checks):
            if isinstance(item, dict) and "command" in item:
                _validate_command_value(item["command"], field=f"checks[{index}].command")
    requirements = value.get("configuration_requirements", [])
    if isinstance(requirements, list):
        for index, item in enumerate(requirements):
            if isinstance(item, dict) and "check" in item:
                _validate_command_value(item["check"], field=f"configuration_requirements[{index}].check")
    for component in value.get("components", []):
        if not isinstance(component, dict):
            continue
        identifier = component.get("id", "component")
        for key in ("tests", "build", "checks"):
            if key in component:
                _validate_declared_commands(component[key], field=f"{identifier}.{key}")
        for recipe_key in ("deployment_recipe", "adoption_recipe", "recovery_recipe"):
            recipe = component.get(recipe_key)
            if not isinstance(recipe, dict):
                continue
            for command_key, command in recipe.items():
                if command_key == "command" or command_key.endswith("_command") or command_key == "check":
                    _validate_command_value(command, field=f"{identifier}.{recipe_key}.{command_key}")


def _validate_release_checks(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ReleaseError("checks must be a list of executable commands or descriptive notes")
    for index, item in enumerate(value):
        field = f"checks[{index}]"
        if not isinstance(item, dict):
            raise ReleaseError(f"{field} must explicitly be {{command: [...]}} or {{note: ...}}")
        has_command = "command" in item
        has_note = "note" in item
        if has_command == has_note:
            raise ReleaseError(f"{field} must contain exactly one of command or note")
        if has_command:
            _validate_direct_argv(item["command"], field=f"{field}.command")
        elif not isinstance(item["note"], str) or not item["note"].strip():
            raise ReleaseError(f"{field}.note must be a non-empty string")


def _validate_configuration_requirements(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ReleaseError("configuration_requirements must be a list of executable requirements")
    for index, item in enumerate(value):
        field = f"configuration_requirements[{index}]"
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ReleaseError(f"{field} must name an executable configuration requirement")
        _validate_direct_argv(item.get("check"), field=f"{field}.check")


def validate_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError(f"unsupported release manifest schema: {value.get('schema_version')!r}")
    release_id = value.get("release_id")
    if not isinstance(release_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", release_id):
        raise ReleaseError("release_id must be lowercase kebab-case, at most 96 characters")
    installation = value.get("installation")
    if not isinstance(installation, dict) or not installation.get("id"):
        raise ReleaseError("installation.id is required")
    for option in ("sudo_systemd", "allow_stopped_service"):
        if option in installation and type(installation[option]) is not bool:
            raise ReleaseError(f"installation.{option} must be a boolean")
    if installation.get("lock_path") is not None and (
        not isinstance(installation["lock_path"], str) or not Path(installation["lock_path"]).is_absolute()
    ):
        raise ReleaseError("installation.lock_path must be an absolute destination path")
    if "session_backup" in installation:
        from .session_backup import validate_backup_selection
        validate_backup_selection(installation["session_backup"])
    migration = value.get("migration", {})
    if not isinstance(migration, dict):
        raise ReleaseError("migration must be an object")
    if migration.get("required"):
        raise UnsupportedAdapter(
            "required migrations are not supported by release-worker; execute a separately reviewed migration lifecycle before planning deployment"
        )
    _validate_release_checks(value.get("checks", []))
    _validate_configuration_requirements(value.get("configuration_requirements", []))
    components = value.get("components")
    if not isinstance(components, list) or not components:
        raise ReleaseError("components must be a non-empty list")
    ids: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise ReleaseError("each component must be an object")
        identifier = component.get("id")
        category = component.get("category")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", identifier):
            raise ReleaseError("component id must be lowercase and stable")
        if identifier in ids:
            raise ReleaseError(f"duplicate component id: {identifier}")
        ids.add(identifier)
        if category not in COMPONENT_CATEGORIES:
            raise ReleaseError(f"unsupported component category {category!r} for {identifier}")
        if not component.get("repository"):
            raise ReleaseError(f"component {identifier} has no repository path")
        if "experience_binding" in component:
            from .experience_binding import validate_selection
            validate_selection(component["experience_binding"], component, installation)
        if "unchanged" in component and not isinstance(component["unchanged"], bool):
            raise ReleaseError(f"{identifier}.unchanged must be a boolean")
        if component.get("unchanged") and category not in {"core", "dashboard"}:
            raise ReleaseError(f"{identifier}.unchanged is only supported for pinned runtime inputs")
        _require_branch(component.get("target_branch", "main"), field=f"{identifier}.target_branch", allow_target=True)
        adapter_name = component.get("adapter", ADAPTERS[category])
        if adapter_name not in KNOWN_ADAPTERS:
            raise UnsupportedAdapter(f"component {identifier}: adapter recipe {adapter_name!r} is not registered")
        recipe_value = component.get("deployment_recipe")
        if "local_integration" in component or (isinstance(recipe_value, dict) and "sudo_artifact_placement" in recipe_value):
            from .local_integration import validate_component
            validate_component(component, installation)
        if category in {"engine", "outreach"}:
            recipe = component.get("deployment_recipe")
            if not isinstance(recipe, dict):
                raise UnsupportedAdapter(f"component {identifier}: independent engine needs deployment_recipe; dashboard host is not inferred")
            missing = {"service", "release_root", "health", "backup", "configuration", "deploy_command", "receipt_path", "health_command", "inspect_command"} - set(recipe)
            if missing:
                raise UnsupportedAdapter(f"component {identifier}: engine recipe is missing {sorted(missing)}")
            if not isinstance(recipe["deploy_command"], list) or not any("{release}" in str(item) for item in recipe["deploy_command"]):
                raise UnsupportedAdapter(f"component {identifier}: deployment_recipe.deploy_command must explicitly target {{release}}")
            for field in ("health_command", "inspect_command"):
                if not isinstance(recipe[field], list) or not any("{release}" in str(item) for item in recipe[field]):
                    raise UnsupportedAdapter(f"component {identifier}: deployment_recipe.{field} must explicitly target {{release}}")
        if category in {"core", "dashboard"}:
            missing = {"host", "port", "service", "release_root", "database", "private_config"} - set(installation)
            if missing:
                raise UnsupportedAdapter(f"component {identifier}: Qorqut installation config is missing {sorted(missing)}")
            if int(installation["port"]) != 1071:
                raise UnsupportedAdapter(f"component {identifier}: production adapter requires the current port 1071, not {installation['port']}")
            launcher = installation.get("launcher")
            if not isinstance(launcher, list) or not launcher or any(not isinstance(item, str) or not item for item in launcher):
                raise UnsupportedAdapter(f"component {identifier}: installation.launcher must name the complete release executable argv")
            if not any("{release}" in item for item in launcher):
                raise UnsupportedAdapter(f"component {identifier}: installation.launcher must explicitly target {{release}}")
            stopper = installation.get("stopper")
            if not isinstance(stopper, list) or not stopper or any(not isinstance(item, str) or not item for item in stopper):
                raise UnsupportedAdapter(f"component {identifier}: installation.stopper must name the complete release stop argv")
            if not any("{release}" in item for item in stopper):
                raise UnsupportedAdapter(f"component {identifier}: installation.stopper must explicitly target {{release}}")
            runtime_required = {"runtime_venv_source", "runtime_install_command", "runtime_validate_command"}
            if runtime_required - set(installation):
                raise UnsupportedAdapter(f"component {identifier}: Qorqut runtime assembly is missing {sorted(runtime_required - set(installation))}")
        dependencies = component.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise ReleaseError(f"{identifier}.dependencies must be a list of component ids")
        config = component.get("configuration", {})
        if not isinstance(config, dict):
            raise ReleaseError(f"{identifier}.configuration must be an object")
        for key in config:
            if re.search(r"token|secret|password|credential|private[_-]?key", key, re.I):
                raise ReleaseError(f"{identifier}.configuration may name secret requirements but not {key}")
        if "build_outputs" in component:
            outputs = component["build_outputs"]
            if not isinstance(outputs, list):
                raise ReleaseError(f"{identifier}.build_outputs must be a list")
            for output in outputs:
                path = output.get("path") if isinstance(output, dict) else output
                if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
                    raise ReleaseError(f"{identifier}.build_outputs must contain repository-relative paths")
        if "checks" in component:
            checks = component["checks"]
            if not isinstance(checks, list):
                raise ReleaseError(f"{identifier}.checks must be a list of direct commands")
            for index, check in enumerate(checks):
                if isinstance(check, dict):
                    _validate_direct_argv(check.get("command"), field=f"{identifier}.checks[{index}].command")
                elif not isinstance(check, str) or not check.strip():
                    raise ReleaseError(f"{identifier}.checks[{index}] must be a command")
    for component in components:
        missing = set(component.get("dependencies", [])) - ids
        if missing:
            raise ReleaseError(f"{component['id']} depends on unknown component(s): {sorted(missing)}")
    runtime_categories = [component["category"] for component in components if component["category"] in {"core", "dashboard"}]
    if runtime_categories and (
        runtime_categories.count("core") != 1 or runtime_categories.count("dashboard") != 1
    ):
        raise UnsupportedAdapter("runtime plans must contain exactly one core and one dashboard component; pin unchanged inputs explicitly")
    if value.get("adoption") is not None and not isinstance(value["adoption"], list):
        raise ReleaseError("adoption must be a list of explicit selections")
    by_id = {item["id"]: item for item in components}
    for selection in value.get("adoption", []):
        if not isinstance(selection, dict) or not isinstance(selection.get("component"), str):
            raise ReleaseError("each adoption selection must name a component")
        selected = by_id.get(selection["component"])
        if not selected or selected["category"] not in {"role", "plugin"}:
            raise ReleaseError(f"adoption selection must target a role or plugin: {selection.get('component')}")
        if not isinstance(selection.get("agents"), list) or not selection["agents"] or any(not isinstance(agent, str) for agent in selection["agents"]):
            raise ReleaseError(f"adoption selection for {selection['component']} must name selected agents")
        if not isinstance(selected.get("adoption_recipe"), dict):
            raise UnsupportedAdapter(
                f"component {selected['id']}: canonical package publication/adoption is unsupported without an explicit adoption_recipe"
            )
        missing = {"command", "receipt_path", "release_root"} - set(selected["adoption_recipe"])
        if missing:
            raise UnsupportedAdapter(f"component {selected['id']}: adoption_recipe is missing {sorted(missing)}")
        if not isinstance(selected["adoption_recipe"]["command"], list) or not any(
            "{artifact}" in str(item) for item in selected["adoption_recipe"]["command"]
        ):
            raise UnsupportedAdapter(
                f"component {selected['id']}: adoption_recipe.command must explicitly target {{artifact}}"
            )
    _validate_command_secrets(value)
    return value


def _topological(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in components}
    remaining = {item["id"]: set(item.get("dependencies", [])) for item in components}
    ordered: list[dict[str, Any]] = []
    while remaining:
        ready = sorted(identifier for identifier, deps in remaining.items() if not deps)
        if not ready:
            raise ReleaseError(f"component dependency cycle: {sorted(remaining)}")
        for identifier in ready:
            ordered.append(by_id[identifier])
            remaining.pop(identifier)
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


def resolve_component(component: dict[str, Any]) -> dict[str, Any]:
    if "local_integration" in component:
        # Validated on the raw path: resolving first could hide a symlink.
        from .local_integration import resolve
        return resolve(component)
    repo = Path(component["repository"]).expanduser().resolve()
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise ReleaseError(f"component {component['id']} repository is not a Git checkout: {repo}")
    source_ref = component.get("source_ref") or component.get("branch") or "HEAD"
    source_leaf = str(source_ref).removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")
    if source_leaf in HISTORY_REFS:
        raise ReleaseError(f"{component['id']}: system data-history ref is not a development source")
    source_commit = component.get("source_commit")
    if source_commit and not SHA.fullmatch(source_commit):
        raise ReleaseError(f"{component['id']}.source_commit must be a full 40-character SHA")
    source_commit = source_commit or _git(repo, "rev-parse", source_ref)
    if not SHA.fullmatch(source_commit):
        raise ReleaseError(f"could not resolve an exact source commit for {component['id']}")
    target_branch = component.get("target_branch", "main")
    try:
        base_commit = _git(repo, "rev-parse", f"refs/remotes/origin/{target_branch}")
    except ReleaseError:
        base_commit = _git(repo, "rev-parse", target_branch)
    status = _git(repo, "status", "--porcelain")
    branch = _git(repo, "branch", "--show-current")
    resolved = dict(component)
    resolved.update(
        {
            "repository": str(repo),
            "source_ref": source_ref,
            "source_commit": source_commit,
            "target_branch": target_branch,
            "base_commit": base_commit,
            "checked_out_branch": branch,
            "dirty": bool(status),
            "adapter": component.get("adapter", ADAPTERS[component["category"]]),
        }
    )
    if "experience_binding" in resolved:
        from .experience_binding import contract_bytes
        selection = resolved["experience_binding"]
        result = subprocess.run(["git", "-C", str(repo), "show", f"{source_commit}:experiences/contract.json"], capture_output=True, check=False)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != selection["contract_sha256"]:
            raise ReleaseError("experience sidecar hash does not match the frozen source commit")
        try:
            contract = json.loads(result.stdout)
        except ValueError:
            raise ReleaseError("experience sidecar at source commit is invalid") from None
        session_format = contract.get("session_format") if isinstance(contract, dict) else None
        if type(session_format) is not int or session_format not in (2, 3) or contract != {"schema_version": 1, "channel_id": resolved["id"], "session_format": session_format,
                        "binding": "experiences/sessions", "dataset_layout": f"plugin-experiences/{resolved['id']}/sessions"}:
            raise ReleaseError("unsupported experience sidecar at source commit")
    if resolved.get("unchanged") and source_commit != base_commit:
        raise ReleaseError(f"{component['id']}: unchanged runtime input must pin the current target commit")
    return resolved


def plan_manifest(value: dict[str, Any]) -> dict[str, Any]:
    value = validate_manifest(value)
    components = [resolve_component(item) for item in _topological(value["components"])]
    selections = {item["component"]: item for item in value.get("adoption", [])}
    for component in components:
        if component["id"] in selections:
            component["adoption"] = selections[component["id"]]
    plan = {
        "schema_version": SCHEMA_VERSION,
        "release_id": value["release_id"],
        "installation": value["installation"],
        "installation_digest": digest(value["installation"]),
        "components": components,
        "execution_order": [item["id"] for item in components],
        "checks": value.get("checks", []),
        "migration": value.get("migration", {}),
        "recovery": value.get("recovery", {}),
        "configuration_requirements": value.get("configuration_requirements", []),
        "adoption": value.get("adoption", []),
        "created_from": value.get("source_file"),
    }
    plan["plan_digest"] = digest(plan)
    return plan


def load_manifest(path: Path) -> dict[str, Any]:
    value = load_data(path)
    value["source_file"] = str(path.resolve())
    return validate_manifest(value)


def load_plan(path: Path) -> dict[str, Any]:
    value = load_data(path)
    expected = value.pop("plan_digest", None)
    if not expected or digest(value) != expected:
        raise ReleaseError(f"frozen plan digest is invalid: {path}")
    value["plan_digest"] = expected
    installation_digest = value.get("installation_digest")
    if installation_digest != digest(value.get("installation", {})):
        raise ReleaseError(f"frozen installation configuration digest is invalid: {path}")
    _validate_command_secrets(value)
    return value
