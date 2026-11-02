"""Conservative Git operations used by the common release worker."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import local_integration
from .errors import ReleaseError
from .manifest import HISTORY_REFS


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise ReleaseError(f"git command failed ({result.returncode}): {' '.join(args[:3])}")
    return result.stdout.strip()


def protected_ref(name: str) -> bool:
    leaf = name.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")
    return leaf in HISTORY_REFS or leaf in {"main", "master"}


def _reject_local_intent(component: dict[str, Any], intent: dict[str, Any]) -> None:
    if intent.get("provenance") == local_integration.PROVENANCE:
        raise ReleaseError(f"{component['id']}: local integration evidence cannot be used by a remote-backed component")


def prepare_merge(component: dict[str, Any], *, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise ReleaseError("merge requires --confirm-merge after a recorded merge authorization")
    if local_integration.is_local(component):
        return local_integration.merge_intent(component)
    repo = Path(component["repository"])
    branch = component.get("checked_out_branch")
    target = component["target_branch"]
    if not branch or protected_ref(branch) or branch in {"HEAD", target}:
        raise ReleaseError(f"{component['id']}: merge requires a dedicated development branch")
    if git(repo, "status", "--porcelain"):
        raise ReleaseError(f"{component['id']}: worktree is dirty")
    git(repo, "fetch", "origin", target, branch)
    head = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", f"refs/remotes/origin/{target}")
    if head != component["source_commit"] or base != component["base_commit"]:
        raise ReleaseError(f"{component['id']}: tested source/base changed; integrate and retest")
    remote_head = git(repo, "rev-parse", f"refs/remotes/origin/{branch}", check=False)
    if remote_head != head:
        raise ReleaseError(f"{component['id']}: development branch is not pushed at the tested commit")
    already_merged = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, base], check=False)
    if already_merged.returncode == 0:
        if git(repo, "rev-parse", f"{head}^{{tree}}") != git(repo, "rev-parse", f"{base}^{{tree}}"):
            raise ReleaseError(f"{component['id']}: already-merged source differs from current target; re-plan against current content")
        return {
            "merge_commit": base,
            "already_merged": True,
            "target": target,
            "source_branch": branch,
            "source_commit": head,
            "base_commit": base,
        }
    ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, head], check=False)
    if ancestry.returncode:
        raise ReleaseError(f"{component['id']}: tested branch is not based on tested target")
    merge_commit = git(repo, "commit-tree", f"{head}^{{tree}}", "-p", base, "-p", head, "-m", f"Merge {branch} into {target}")
    return {
        "merge_commit": merge_commit,
        "target": target,
        "source_branch": branch,
        "source_commit": head,
        "base_commit": base,
    }


def merge_component(
    component: dict[str, Any],
    *,
    confirm: bool,
    intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise ReleaseError("merge requires --confirm-merge after a recorded merge authorization")
    if local_integration.is_local(component):
        # Read-only verification of the already completed local integration;
        # nothing here may fetch, push or write a ref or object.
        return local_integration.merge_result(component, intent)
    if intent is None:
        intent = (
            {
                "unchanged": True,
                "target": component["target_branch"],
                "source_commit": component["source_commit"],
                "base_commit": component["base_commit"],
            }
            if component.get("unchanged")
            else prepare_merge(component, confirm=confirm)
        )
    _reject_local_intent(component, intent)
    repo = Path(component["repository"])
    branch = component.get("checked_out_branch")
    target = component["target_branch"]
    if intent.get("unchanged"):
        if intent.get("target") != target or intent.get("source_commit") != component["source_commit"] or intent.get("base_commit") != component["base_commit"]:
            raise ReleaseError(f"{component['id']}: unchanged runtime input no longer matches the frozen target")
        git(repo, "fetch", "origin", target)
        head = git(repo, "rev-parse", "HEAD")
        base = git(repo, "rev-parse", f"refs/remotes/origin/{target}")
        if head != component["source_commit"] or base != component["base_commit"] or head != base:
            raise ReleaseError(f"{component['id']}: unchanged runtime input moved; re-plan before continuing")
        return {"merged": False, "unchanged": True, **intent}
    if not branch or protected_ref(branch) or branch in {"HEAD", target}:
        raise ReleaseError(f"{component['id']}: merge requires a dedicated development branch")
    if git(repo, "status", "--porcelain"):
        raise ReleaseError(f"{component['id']}: worktree is dirty")
    git(repo, "fetch", "origin", target, branch)
    head = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", f"refs/remotes/origin/{target}")
    remote_head = git(repo, "rev-parse", f"refs/remotes/origin/{branch}", check=False)
    if (
        intent.get("merge_commit") is None
        or intent.get("target") != target
        or intent.get("source_branch") != branch
        or intent.get("source_commit") != head
        or intent.get("base_commit") != base
        or head != component["source_commit"]
        or base != component["base_commit"]
    ):
        raise ReleaseError(f"{component['id']}: merge inputs changed after the merge intent was recorded")
    if remote_head != head:
        raise ReleaseError(f"{component['id']}: development branch is not pushed at the tested commit")
    if intent.get("already_merged"):
        ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, base], check=False)
        if (
            intent["merge_commit"] != base
            or ancestry.returncode
            or git(repo, "rev-parse", f"{head}^{{tree}}") != git(repo, "rev-parse", f"{base}^{{tree}}")
        ):
            raise ReleaseError(f"{component['id']}: already-merged source is not the frozen target content")
        return {"merged": False, **intent}
    ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, head], check=False)
    if ancestry.returncode:
        raise ReleaseError(f"{component['id']}: tested branch is not based on tested target")
    object_check = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{intent['merge_commit']}^{{commit}}"],
        check=False,
    )
    if object_check.returncode:
        raise ReleaseError(f"{component['id']}: recorded merge commit object is unavailable; reconcile before retrying")
    pushed = subprocess.run(["git", "-C", str(repo), "push", "origin", f"{intent['merge_commit']}:refs/heads/{target}"], text=True, capture_output=True, check=False)
    if pushed.returncode:
        raise ReleaseError(f"{component['id']}: target moved or push failed; no force update was attempted")
    git(repo, "fetch", "origin", target)
    verified = git(repo, "rev-parse", f"refs/remotes/origin/{target}")
    if verified != intent["merge_commit"]:
        raise ReleaseError(f"{component['id']}: remote target did not land the exact merge commit")
    return {"merged": True, **intent}


def reconcile_merge(component: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a merge whose push or post-push journal write was interrupted."""

    if local_integration.is_local(component):
        return local_integration.reconcile_merge(component, intent)
    _reject_local_intent(component, intent)
    repo = Path(component["repository"])
    target = str(intent.get("target", component.get("target_branch", "")))
    merge_commit = str(intent.get("merge_commit", ""))
    if intent.get("unchanged"):
        return {"already_succeeded": True, "safe_to_retry": False, "unchanged": True}
    if not target or not merge_commit:
        return {"safe_to_retry": False, "reason": "merge intent is incomplete"}
    git(repo, "fetch", "origin", target)
    remote_target = git(repo, "rev-parse", f"refs/remotes/origin/{target}")
    if remote_target == merge_commit:
        return {"already_succeeded": True, "safe_to_retry": False, "remote_target": remote_target, "merge_commit": merge_commit}
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", merge_commit, remote_target],
        check=False,
    )
    if ancestor.returncode == 0:
        return {"already_succeeded": True, "safe_to_retry": False, "remote_target": remote_target, "merge_commit": merge_commit}
    if remote_target == str(intent.get("base_commit", "")):
        return {"safe_to_retry": True, "remote_target": remote_target, "merge_commit": merge_commit, "reason": "remote target is still the recorded base"}
    return {"safe_to_retry": False, "remote_target": remote_target, "merge_commit": merge_commit, "reason": "remote target advanced without the recorded merge commit"}


def _common_repo(repo: Path) -> Path:
    """Return the repository that administers ``repo``'s worktrees and branches.

    That is the common Git directory itself for a bare repository (for example
    ``project.git`` with worktrees under ``worktrees/<task>/``), otherwise
    the primary checkout containing ``.git``.
    """

    common = Path(git(repo, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = repo / common
    common = common.resolve()
    if git(common, "rev-parse", "--is-bare-repository") == "true":
        return common
    if common.name != ".git":
        raise ReleaseError(f"unsupported Git layout for {repo}: {common} is neither bare nor a checkout's .git")
    return common.parent


def _require_common_repo(component: dict[str, Any], common_repo: Path) -> None:
    if _common_repo(common_repo) != common_repo.resolve():
        raise ReleaseError(f"{component['id']}: {common_repo} is no longer the repository that owns the cleanup worktree")


def _fetch_target(repo: Path, target: str) -> str:
    # Explicit refspec: a plain bare clone has no fetch refspec that would update the tracking ref.
    tracking = f"refs/remotes/origin/{target}"
    git(repo, "fetch", "origin", f"+refs/heads/{target}:{tracking}")
    return tracking


def _is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, ref], check=False).returncode == 0


def _delete_merged_local_branch(component: dict[str, Any], common_repo: Path, branch: str, head: str, target: str) -> None:
    """Delete ``branch`` only at its approved tip once that tip is merged into origin/<target>.

    ``git branch -d`` compares against the branch's upstream, or without one
    against the administering repository's HEAD, which in a bare repository is
    a local branch nobody updates.
    """

    ref = f"refs/heads/{branch}"
    if git(common_repo, "rev-parse", "--verify", "--quiet", ref, check=False) != head:
        raise ReleaseError(f"{component['id']}: local branch {branch} moved after cleanup was approved; it was kept")
    checked_out = {entry.get("branch") for entry in _worktree_entries(common_repo)}
    checked_out.add(git(common_repo, "symbolic-ref", "--quiet", "HEAD", check=False))
    if ref in checked_out:
        raise ReleaseError(f"{component['id']}: local branch {branch} is still checked out; it was kept")
    if not _is_ancestor(common_repo, head, _fetch_target(common_repo, target)):
        raise ReleaseError(f"{component['id']}: local branch {branch} is not merged into origin/{target}; it was kept")
    git(common_repo, "update-ref", "-d", ref, head)
    # Match `git branch -D`, which also drops the branch's upstream configuration.
    git(common_repo, "config", "--remove-section", f"branch.{branch}", check=False)


def _worktree_entries(common_repo: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in git(common_repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key in {"worktree", "HEAD", "branch"}:
            current[key] = value
    return entries


def _current_worktree_entry(common_repo: Path, worktree: Path) -> dict[str, str]:
    expected = worktree.resolve()
    for entry in _worktree_entries(common_repo):
        if Path(entry.get("worktree", "")).resolve() == expected:
            return entry
    raise ReleaseError(f"worktree is no longer registered with its Git repository: {worktree}")


def _remote_branch_tip(common_repo: Path, branch: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(common_repo), "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 2:
        return None
    if result.returncode:
        raise ReleaseError(f"could not inspect remote branch {branch}: {result.stderr.strip()[-400:]}")
    lines = result.stdout.splitlines()
    if len(lines) != 1 or len(lines[0].split()) < 2:
        raise ReleaseError(f"remote branch {branch} returned an invalid tip")
    return lines[0].split()[0]


def _verify_cleanup_worktree(component: dict[str, Any], intent: dict[str, Any]) -> None:
    worktree = Path(str(intent["worktree"]))
    common_repo = Path(str(intent["common_repo"]))
    expected_branch = str(intent["branch"])
    expected_head = intent.get("head")
    if not isinstance(expected_head, str) or not expected_head:
        raise ReleaseError(f"{component['id']}: cleanup intent has no recorded HEAD")
    if not worktree.exists():
        return
    if worktree.resolve() == common_repo.resolve():
        raise ReleaseError(f"{component['id']}: refusing to remove the primary checkout or bare repository")
    if _common_repo(worktree) != common_repo.resolve():
        raise ReleaseError(f"{component['id']}: cleanup worktree no longer belongs to {common_repo}")
    entry = _current_worktree_entry(common_repo, worktree)
    branch = git(worktree, "branch", "--show-current")
    head = git(worktree, "rev-parse", "HEAD")
    recorded_branch = entry.get("branch", "").removeprefix("refs/heads/")
    if branch != expected_branch or recorded_branch != expected_branch:
        raise ReleaseError(
            f"{component['id']}: cleanup worktree branch changed from {expected_branch!r}"
        )
    if head != expected_head or entry.get("HEAD") != expected_head:
        raise ReleaseError(f"{component['id']}: cleanup worktree HEAD changed after planning")
    if git(worktree, "status", "--porcelain"):
        raise ReleaseError(f"{component['id']}: dirty worktree will not be removed")


def prepare_cleanup(component: dict[str, Any], *, confirm: bool) -> dict[str, Any]:
    if local_integration.is_local(component):
        local_integration.reject_cleanup(component)
    if not confirm:
        raise ReleaseError("cleanup requires --confirm-cleanup")
    repo = Path(component["repository"])
    expected_branch = component.get("checked_out_branch")
    if not expected_branch or protected_ref(expected_branch):
        raise ReleaseError(f"{component['id']}: protected or unknown branch cannot be cleaned")
    branch = git(repo, "branch", "--show-current")
    if branch != expected_branch:
        raise ReleaseError(f"{component['id']}: current branch is not the planned cleanup branch")
    head = git(repo, "rev-parse", "HEAD")
    if component.get("source_commit") and head != component["source_commit"]:
        raise ReleaseError(f"{component['id']}: current HEAD is not the planned cleanup commit")
    common_repo = _common_repo(repo)
    # The manifest may point at a primary checkout or a bare repository; never remove either.
    if common_repo == repo.resolve():
        raise ReleaseError(f"{component['id']}: refusing to remove the primary checkout or bare repository")
    current_entry = _current_worktree_entry(common_repo, repo)
    if current_entry.get("HEAD") != head or current_entry.get("branch", "").removeprefix("refs/heads/") != branch:
        raise ReleaseError(f"{component['id']}: current worktree identity does not match the planned checkout")
    target = component["target_branch"]
    if not _is_ancestor(repo, head, _fetch_target(repo, target)):
        raise ReleaseError(f"{component['id']}: branch is not merged into origin/{target}")
    remote_head = _remote_branch_tip(common_repo, branch)
    if remote_head != head:
        raise ReleaseError(f"{component['id']}: remote branch tip changed before cleanup")
    return {
        "worktree": str(repo.resolve()),
        "task_directory": str(repo.resolve().parent),
        "common_repo": str(common_repo),
        "branch": branch,
        "head": head,
        "remote_head": remote_head,
        "target": target,
    }


def cleanup_component(
    component: dict[str, Any],
    *,
    confirm: bool,
    intent: dict[str, Any] | None = None,
    phase_callback: Any = None,
) -> dict[str, Any]:
    if local_integration.is_local(component):
        local_integration.reject_cleanup(component)
    if intent is None:
        intent = prepare_cleanup(component, confirm=confirm)
    elif not confirm:
        raise ReleaseError("cleanup requires --confirm-cleanup")
    worktree = Path(str(intent["worktree"]))
    task_directory = Path(str(intent["task_directory"]))
    common_repo = Path(str(intent["common_repo"]))
    branch = str(intent["branch"])
    expected_remote_head = str(intent.get("remote_head", intent["head"]))
    _require_common_repo(component, common_repo)
    initial_remote_head = _remote_branch_tip(common_repo, branch)
    if initial_remote_head is not None and initial_remote_head != expected_remote_head:
        raise ReleaseError(f"{component['id']}: refusing cleanup after the remote branch tip changed")
    phases: dict[str, Any] = {}

    def completed(name: str, value: Any) -> None:
        phases[name] = value
        if phase_callback:
            phase_callback(name, value)

    if worktree.exists():
        _verify_cleanup_worktree(component, intent)
        git(common_repo, "worktree", "remove", str(worktree))
    completed("worktree_removed", {"path": str(worktree)})

    local_branch = subprocess.run(
        ["git", "-C", str(common_repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    if local_branch.returncode == 0:
        target = str(intent.get("target") or component["target_branch"])
        _delete_merged_local_branch(component, common_repo, branch, str(intent["head"]), target)
    elif local_branch.returncode not in {1, 128}:
        raise ReleaseError(f"{component['id']}: could not inspect local branch {branch}")
    else:
        # A cleanup interrupted after `update-ref -d` leaves the upstream configuration behind.
        git(common_repo, "config", "--remove-section", f"branch.{branch}", check=False)
    completed("local_branch_removed", {"branch": branch})

    remote_head = _remote_branch_tip(common_repo, branch)
    if remote_head is not None:
        if remote_head != expected_remote_head:
            raise ReleaseError(f"{component['id']}: refusing to delete a remote branch with a changed tip")
        pushed = subprocess.run(
            [
                "git",
                "-C",
                str(common_repo),
                "push",
                f"--force-with-lease=refs/heads/{branch}:{expected_remote_head}",
                "origin",
                f":refs/heads/{branch}",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if pushed.returncode:
            raise ReleaseError(f"{component['id']}: remote branch deletion failed")
        if _remote_branch_tip(common_repo, branch) is not None:
            raise ReleaseError(f"{component['id']}: remote branch deletion was not verified")
    completed("remote_branch_removed", {"branch": branch})

    if task_directory.exists():
        remaining = list(task_directory.iterdir())
        if remaining:
            completed(
                "task_directory_pending",
                {"path": str(task_directory), "remaining": [str(path) for path in remaining]},
            )
            return {
                "cleaned": False,
                "branch": branch,
                "worktree": str(worktree),
                "task_directory": str(task_directory),
                "task_directory_pending": True,
                "phases": phases,
            }
        task_directory.rmdir()
    completed("task_directory_removed", {"path": str(task_directory)})
    return {
        "cleaned": True,
        "branch": branch,
        "worktree": str(worktree),
        "task_directory": str(task_directory),
        "phases": phases,
    }


def reconcile_cleanup(component: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    """Inspect cleanup progress without deleting anything."""

    if local_integration.is_local(component):
        local_integration.reject_cleanup(component)
    raw_worktree = intent.get("worktree")
    raw_task_directory = intent.get("task_directory")
    raw_common_repo = intent.get("common_repo")
    raw_branch = intent.get("branch")
    raw_head = intent.get("head")
    if any(not isinstance(value, str) or not value.strip() for value in (raw_worktree, raw_task_directory, raw_common_repo, raw_branch, raw_head)):
        return {"safe_to_retry": False, "reason": "cleanup intent is incomplete"}
    worktree = Path(raw_worktree)
    task_directory = Path(raw_task_directory)
    common_repo = Path(raw_common_repo)
    branch = raw_branch
    if not worktree.is_absolute() or not task_directory.is_absolute() or not common_repo.is_absolute() or protected_ref(branch):
        return {"safe_to_retry": False, "reason": "cleanup intent contains unsafe paths or branch"}
    expected_worktree = Path(component["repository"]).resolve()
    if worktree.resolve() != expected_worktree or task_directory != expected_worktree.parent:
        return {"safe_to_retry": False, "reason": "cleanup intent no longer matches the frozen repository"}
    if not common_repo.exists() or not common_repo.is_dir():
        return {"safe_to_retry": False, "reason": "cleanup common repository is unavailable"}
    try:
        _require_common_repo(component, common_repo)
    except ReleaseError as exc:
        return {"safe_to_retry": False, "reason": f"cleanup common repository cannot be inspected: {exc}"}
    worktree_removed = not worktree.exists()
    if not worktree_removed:
        try:
            _verify_cleanup_worktree(component, intent)
        except ReleaseError as exc:
            return {"safe_to_retry": False, "reason": f"cleanup worktree cannot be inspected: {exc}"}
    local_head = git(common_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if local_head and local_head != raw_head:
        return {"safe_to_retry": False, "reason": "local branch tip changed after cleanup was approved"}
    local_present = bool(local_head)
    try:
        remote_head = _remote_branch_tip(common_repo, branch)
    except ReleaseError as exc:
        return {"safe_to_retry": False, "reason": str(exc)}
    if remote_head is not None and remote_head != raw_head:
        return {"safe_to_retry": False, "reason": "remote branch tip changed after cleanup was approved"}
    remote_present = remote_head is not None
    task_present = task_directory.exists()
    remaining = list(task_directory.iterdir()) if task_present else []
    observed = {
        "worktree_removed": worktree_removed,
        "local_branch_present": local_present,
        "remote_branch_present": remote_present,
        "remote_branch_head": remote_head,
        "task_directory_present": task_present,
        "task_directory_remaining": [str(path) for path in remaining],
    }
    if worktree_removed and not local_present and not remote_present and remaining:
        return {
            "already_succeeded": True,
            "safe_to_retry": False,
            "shared_task_directory_pending": True,
            "observed": observed,
            "reason": "component cleanup is complete; shared task directory awaits the other repositories",
        }
    if worktree_removed and not local_present and not remote_present and not task_present:
        return {"already_succeeded": True, "safe_to_retry": False, "observed": observed}
    return {"safe_to_retry": True, "observed": observed, "reason": "cleanup phases remain and are idempotently retryable"}
