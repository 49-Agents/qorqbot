"""Opt-in local source provenance for the independent-engine lifecycle.

A component may declare ``local_integration`` when its independent, remote-less
checkout was genuinely merged into a local integration branch. The declaration
names that branch explicitly (``integrated_ref``, a full ``refs/heads/<branch>``
ref that ``target_branch`` repeats); no target is defaulted or created. The merge
step then verifies that completed integration read-only instead of fetching or
pushing ``origin``. Components without the field keep the existing remote-backed
behavior.

Every function here uses the sanitized, read-only Git helper below: no hooks,
maintenance, lazy fetch, index refresh, ref update or object creation. Any
drift from the frozen plan is an error that needs a new plan.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

from .errors import ReleaseError
from .io import checked_path
from .manifest import BRANCH, HISTORY_REFS, SHA

FIELD = "local_integration"
FIELDS = frozenset({"workspace_root", "integrated_ref"})
HEADS = "refs/heads/"
PROTECTED_BRANCHES = frozenset({"main", "master", "base"})
PROVENANCE = "local-integrated"
PLACEMENT_FIELD = "sudo_artifact_placement"
# Plan facts that must still hold when any lifecycle step revalidates the source.
FROZEN = ("repository", "source_ref", "source_commit", "source_tree", "checked_out_branch", "integrated_ref",
          "target_branch", "base_commit", "target_tree", "provenance", "remote_verified", "git_mutated")


def _environment():
    # No optional index writes, lazy network fetches or user/system configuration.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_COUNT"):
        env.pop(key, None)
    return env


def git(repo, *args, binary=False):
    # No hooks or automatic maintenance either.
    result = subprocess.run(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
        "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
        env=_environment(), capture_output=True, check=False)
    if result.returncode:
        raise ReleaseError(f"Local source verification failed: {args[0]}")
    return result.stdout if binary else result.stdout.decode().strip()


def _protected(branch):
    return branch in PROTECTED_BRANCHES or branch.endswith("/base") or branch in HISTORY_REFS


def _valid_branch(branch):
    # Read-only syntax check; it needs no repository.
    return (bool(BRANCH.fullmatch(branch)) and not branch.startswith("-") and subprocess.run(
        ["git", "check-ref-format", HEADS + branch], env=_environment(), capture_output=True, check=False).returncode == 0)


def declared_branch(ref, field):
    """The branch of an explicit, valid, non-protected full ``refs/heads/<branch>`` ref."""

    if not isinstance(ref, str) or not ref.startswith(HEADS):
        raise ReleaseError(f"{field} must be a full {HEADS}<branch> ref")
    branch = ref[len(HEADS):]
    if not _valid_branch(branch):
        raise ReleaseError(f"{field} is not a valid Git branch ref: {ref!r}")
    if _protected(branch):
        raise ReleaseError(f"{field} may not name the protected branch {branch}")
    return branch


def local_source(selection, workspace):
    if set(selection) != {"repository", "commit", "integrated_ref"}:
        raise ReleaseError("A local component selects repository, exact commit and integrated_ref only")
    integrated_ref = selection["integrated_ref"]
    target = declared_branch(integrated_ref, "integrated_ref")
    workspace = checked_path(workspace)
    if not workspace.is_dir():
        raise ReleaseError("The local release workspace must be an existing directory")
    repo = checked_path(selection["repository"], workspace)
    commit = selection["commit"]
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ReleaseError("Select a full source commit")
    # Read the .git marker without executing Git before checking where it points.
    marker = repo / ".git"
    checked_path(marker, workspace)
    if marker.is_file():
        text = marker.read_text().strip()
        if not text.startswith("gitdir: "):
            raise ReleaseError("Invalid source worktree marker")
        gitdir = Path(os.path.abspath(repo / text[8:]))
    else:
        gitdir = marker
    checked_path(gitdir, workspace)
    common = gitdir
    if (gitdir / "commondir").exists():
        checked_path(gitdir / "commondir", workspace)
        common = Path(os.path.abspath(gitdir / (gitdir / "commondir").read_text().strip()))
    checked_path(common, workspace)
    checked_path(common / "objects", workspace)
    if (common / "objects/info/alternates").exists():
        raise ReleaseError("Local release sources must have independent object stores")
    if git(repo, "remote"):
        raise ReleaseError("Local-only provenance cannot be used for a repository with remotes")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("Local release source must be clean")
    branch = git(repo, "symbolic-ref", "--quiet", "HEAD")
    if not branch.startswith(HEADS) or _protected(branch[len(HEADS):]):
        raise ReleaseError("Source must be on a dedicated, non-protected branch")
    if branch == integrated_ref:
        raise ReleaseError(f"the source branch must differ from {target}")
    if (git(repo, "rev-parse", "HEAD") != commit
            or git(repo, "merge-base", commit, integrated_ref) != commit):
        raise ReleaseError("Selected source must equal the frozen commit already merged into local integration")
    return {**selection, "branch": branch, "tree": git(repo, "rev-parse", commit + "^{tree}"),
            "provenance": PROVENANCE, "remote_verified": False, "git_mutated": False}


def is_local(component: dict[str, Any]) -> bool:
    return FIELD in component


def placement_enabled(component: dict[str, Any]) -> bool:
    recipe = component.get("deployment_recipe")
    return isinstance(recipe, dict) and recipe.get(PLACEMENT_FIELD) is True


def _normalized(value: Any, field: str, root: Path | None = None) -> Path:
    if not isinstance(value, str) or not value:
        raise ReleaseError(f"{field} must be a normalized absolute path")
    # Checked on the raw path, so a symlink is rejected before anything resolves it.
    path = checked_path(value, root)
    if str(path) != value:
        raise ReleaseError(f"{field} must be a normalized absolute path")
    return path


def validate_declaration(component: dict[str, Any]) -> tuple[Path, str, str]:
    """Statically validate ``local_integration``.

    Returns the workspace root, the declared ``integrated_ref`` and its branch,
    which ``target_branch`` must repeat. Every later step derives the target here.
    """

    identifier = component.get("id", "component")
    selection = component.get(FIELD)
    if not isinstance(selection, dict) or set(selection) != FIELDS:
        raise ReleaseError(f"{identifier}.{FIELD} requires exactly workspace_root and integrated_ref")
    integrated_ref = selection["integrated_ref"]
    target = declared_branch(integrated_ref, f"{identifier}.{FIELD}.integrated_ref")
    if component.get("category") != "engine" or component.get("adapter", "independent-engine") != "independent-engine":
        raise ReleaseError(f"{identifier}: {FIELD} is only supported for the engine category with the independent-engine adapter")
    if component.get("target_branch") != target:
        raise ReleaseError(f"{identifier}: {FIELD} requires target_branch {target} (the integrated_ref branch)")
    if not isinstance(component.get("source_commit"), str) or not SHA.fullmatch(component["source_commit"]):
        raise ReleaseError(f"{identifier}: {FIELD} requires an explicit full source_commit")
    source_ref = component.get("source_ref") or component.get("branch") or "HEAD"
    leaf = source_ref.removeprefix(HEADS) if isinstance(source_ref, str) else ""
    if not isinstance(source_ref, str) or (source_ref != "HEAD" and (not _valid_branch(leaf) or _protected(leaf))):
        raise ReleaseError(f"{identifier}: {FIELD} source_ref must name the dedicated source branch or HEAD")
    if leaf == target:
        raise ReleaseError(f"{identifier}: {FIELD} source_ref must differ from {target}")
    workspace = _normalized(selection["workspace_root"], f"{identifier}.{FIELD}.workspace_root")
    _normalized(component.get("repository"), f"{identifier}.repository", workspace)
    return workspace, integrated_ref, target


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

    workspace, integrated_ref, target = validate_declaration(component)
    commit = component["source_commit"]
    source = local_source({"repository": component["repository"], "commit": commit,
                           "integrated_ref": integrated_ref}, workspace)
    repo = Path(component["repository"])
    source_ref = component.get("source_ref") or component.get("branch") or "HEAD"
    if git(repo, "rev-parse", "--verify", "--quiet", source_ref + "^{commit}") != commit:
        raise ReleaseError(f"{component['id']}: source_ref no longer resolves to the selected source commit")
    tip = git(repo, "rev-parse", "--verify", "--quiet", integrated_ref + "^{commit}")
    target_tree = git(repo, "rev-parse", "--verify", "--quiet", integrated_ref + "^{tree}")
    if source["tree"] != target_tree:
        raise ReleaseError(f"{component['id']}: local {target} content differs from the selected source; "
                           "integrate exactly the source and create a new plan")
    return {"repository": component["repository"], "source_ref": source_ref, "source_commit": commit,
            "source_tree": source["tree"], "checked_out_branch": source["branch"].removeprefix(HEADS),
            "integrated_ref": integrated_ref, "target_branch": target, "base_commit": tip,
            "target_tree": target_tree, "provenance": PROVENANCE, "remote_verified": False, "git_mutated": False}


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
            "remote_verified": False, "git_mutated": False, "target": facts["target_branch"],
            "integrated_ref": facts["integrated_ref"], "source_branch": facts["checked_out_branch"],
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
