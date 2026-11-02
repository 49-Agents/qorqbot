"""Opt-in local source provenance for the independent-engine lifecycle.

A component may declare ``local_integration`` when its independent, remote-less
checkout was genuinely merged into the local ``v1.2/integration`` branch. The
merge step then verifies that completed integration read-only instead of
fetching or pushing ``origin``. Components without the field keep the existing
remote-backed behavior.

Every function here uses the sanitized, read-only Git helper of the v1.2 local
recipe: no hooks, maintenance, lazy fetch, index refresh, ref update or object
creation. Any drift from the frozen plan is an error that needs a new plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import v12
from .errors import ReleaseError
from .manifest import BRANCH, HISTORY_REFS, SHA

FIELD = "local_integration"
FIELDS = frozenset({"workspace_root", "integrated_ref"})
INTEGRATED_REF = "refs/heads/v1.2/integration"
TARGET_BRANCH = "v1.2/integration"
PROVENANCE = "local-integrated"
PLACEMENT_FIELD = "sudo_artifact_placement"
# Plan facts that must still hold when any lifecycle step revalidates the source.
FROZEN = ("repository", "source_ref", "source_commit", "source_tree", "checked_out_branch",
          "target_branch", "base_commit", "target_tree", "provenance", "remote_verified", "git_mutated")


def is_local(component: dict[str, Any]) -> bool:
    return FIELD in component


def placement_enabled(component: dict[str, Any]) -> bool:
    recipe = component.get("deployment_recipe")
    return isinstance(recipe, dict) and recipe.get(PLACEMENT_FIELD) is True


def _normalized(value: Any, field: str, root: Path | None = None) -> Path:
    if not isinstance(value, str) or not value:
        raise ReleaseError(f"{field} must be a normalized absolute path")
    # Checked on the raw path, so a symlink is rejected before anything resolves it.
    path = v12.checked_path(value, root)
    if str(path) != value:
        raise ReleaseError(f"{field} must be a normalized absolute path")
    return path


def validate_declaration(component: dict[str, Any]) -> Path:
    """Statically validate ``local_integration`` and return its workspace root."""

    identifier = component.get("id", "component")
    selection = component.get(FIELD)
    if not isinstance(selection, dict) or set(selection) != FIELDS:
        raise ReleaseError(f"{identifier}.{FIELD} requires exactly workspace_root and integrated_ref")
    if selection["integrated_ref"] != INTEGRATED_REF:
        raise ReleaseError(f"{identifier}.{FIELD}.integrated_ref must be {INTEGRATED_REF}")
    if component.get("category") != "engine" or component.get("adapter", "independent-engine") != "independent-engine":
        raise ReleaseError(f"{identifier}: {FIELD} is only supported for the engine category with the independent-engine adapter")
    if component.get("target_branch") != TARGET_BRANCH:
        raise ReleaseError(f"{identifier}: {FIELD} requires target_branch {TARGET_BRANCH}")
    if component.get("unchanged"):
        raise ReleaseError(f"{identifier}: {FIELD} cannot be combined with unchanged")
    if not isinstance(component.get("source_commit"), str) or not SHA.fullmatch(component["source_commit"]):
        raise ReleaseError(f"{identifier}: {FIELD} requires an explicit full source_commit")
    source_ref = component.get("source_ref") or component.get("branch") or "HEAD"
    leaf = source_ref.removeprefix("refs/heads/") if isinstance(source_ref, str) else ""
    if (not isinstance(source_ref, str) or not BRANCH.fullmatch(source_ref) or source_ref.startswith("-")
            or leaf in HISTORY_REFS or leaf in {"main", "master"}):
        raise ReleaseError(f"{identifier}: {FIELD} source_ref must name the dedicated source branch or HEAD")
    workspace = _normalized(selection["workspace_root"], f"{identifier}.{FIELD}.workspace_root")
    _normalized(component.get("repository"), f"{identifier}.repository", workspace)
    return workspace


def validate_placement(component: dict[str, Any], installation: dict[str, Any]) -> None:
    """Reject the privileged-copy flag outside explicitly local engine mode."""

    recipe = component.get("deployment_recipe")
    if not isinstance(recipe, dict) or PLACEMENT_FIELD not in recipe:
        return
    identifier = component.get("id", "component")
    if type(recipe[PLACEMENT_FIELD]) is not bool:
        raise ReleaseError(f"{identifier}.deployment_recipe.{PLACEMENT_FIELD} must be a boolean")
    if not is_local(component):
        raise ReleaseError(f"{identifier}.deployment_recipe.{PLACEMENT_FIELD} is only supported with {FIELD}")
    if installation.get("host") != "localhost" or recipe.get("host") != "localhost":
        raise ReleaseError(f"{identifier}.deployment_recipe.{PLACEMENT_FIELD} requires installation.host and the recipe host to be localhost")


def validate_component(component: dict[str, Any], installation: dict[str, Any]) -> None:
    if is_local(component):
        validate_declaration(component)
    validate_placement(component, installation)


def _facts(component: dict[str, Any]) -> dict[str, Any]:
    """Read the current local facts, enforcing every provenance rule."""

    workspace = validate_declaration(component)
    commit = component["source_commit"]
    source = v12.local_source({"repository": component["repository"], "commit": commit,
                               "integrated_ref": component[FIELD]["integrated_ref"]}, workspace)
    repo = Path(component["repository"])
    branch = v12.git(repo, "symbolic-ref", "--quiet", "HEAD")
    if branch == INTEGRATED_REF:
        raise ReleaseError(f"{component['id']}: the source branch must differ from {TARGET_BRANCH}")
    source_ref = component.get("source_ref") or component.get("branch") or "HEAD"
    if v12.git(repo, "rev-parse", "--verify", "--quiet", source_ref + "^{commit}") != commit:
        raise ReleaseError(f"{component['id']}: source_ref no longer resolves to the selected source commit")
    target = v12.git(repo, "rev-parse", "--verify", "--quiet", INTEGRATED_REF + "^{commit}")
    target_tree = v12.git(repo, "rev-parse", "--verify", "--quiet", INTEGRATED_REF + "^{tree}")
    if source["tree"] != target_tree:
        raise ReleaseError(f"{component['id']}: local {TARGET_BRANCH} content differs from the selected source; "
                           "integrate exactly the source and create a new plan")
    return {"repository": component["repository"], "source_ref": source_ref, "source_commit": commit,
            "source_tree": source["tree"], "checked_out_branch": branch.removeprefix("refs/heads/"),
            "target_branch": TARGET_BRANCH, "base_commit": target, "target_tree": target_tree,
            "provenance": PROVENANCE, "remote_verified": False, "git_mutated": False}


def resolve(component: dict[str, Any]) -> dict[str, Any]:
    """Freeze a component for planning; the repository path is never resolved first."""

    facts = _facts(component)
    return {**component, **facts, "dirty": False, "adapter": "independent-engine"}


def verify_frozen(component: dict[str, Any]) -> dict[str, Any]:
    """Require the planned source, branch and integration tip/tree to be unchanged."""

    facts = _facts(component)
    for key in FROZEN:
        if component.get(key) != facts[key]:
            raise ReleaseError(f"{component['id']}: local {key} changed after planning; create a new plan")
    return facts


def merge_intent(component: dict[str, Any]) -> dict[str, Any]:
    """Describe the already completed integration; no ref or object is written."""

    facts = verify_frozen(component)
    return {"merged": False, "already_integrated": True, "provenance": PROVENANCE,
            "remote_verified": False, "git_mutated": False, "target": TARGET_BRANCH,
            "integrated_ref": INTEGRATED_REF, "source_branch": facts["checked_out_branch"],
            "source_commit": facts["source_commit"], "source_tree": facts["source_tree"],
            "base_commit": facts["base_commit"], "target_tree": facts["target_tree"],
            "merge_commit": facts["base_commit"]}


def merge_result(component: dict[str, Any], intent: dict[str, Any] | None) -> dict[str, Any]:
    expected = merge_intent(component)
    if intent is not None and intent != expected:
        raise ReleaseError(f"{component['id']}: local merge intent no longer matches the frozen plan and current integration; create a new plan")
    return expected


def reconcile_merge(component: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    try:
        result = merge_result(component, intent)
    except ReleaseError as exc:
        return {"safe_to_retry": False, "reason": f"local integration does not match the frozen plan: {exc}"}
    # No destination or Git effect happened; this also supersedes the interrupted attempt's "unknown".
    return {"already_succeeded": True, "safe_to_retry": False, "external_effect": False, **result}


def reject_cleanup(component: dict[str, Any]) -> None:
    raise ReleaseError(f"{component['id']}: cleanup is unsupported for local-integrated sources; "
                       "the source checkout and worktree are retained")
