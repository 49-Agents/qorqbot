"""Local-source provenance and workspace-path checks of the release worker's local integration helpers."""
from pathlib import Path
import subprocess

import pytest

from bazarlyk.release_worker.errors import ReleaseError
from bazarlyk.release_worker.io import checked_path
from bazarlyk.release_worker.local_integration import local_source

INTEGRATION = "refs/heads/release/integration"


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
        "-C", str(repo), *args], text=True).strip()


def repository(root, name):
    """A source on its own work branch whose commit is also the tip of the integration branch."""

    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "release/integration")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "fixture")
    (repo / "source.py").write_text("# fixture\n")
    git(repo, "add", "source.py")
    git(repo, "commit", "-qm", "fixture")
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "main", commit)
    git(repo, "checkout", "-q", "-b", "work/source")
    return {"repository": str(repo), "commit": commit, "integrated_ref": INTEGRATION}


def test_local_provenance_requires_exact_integration_without_mutating_refs(tmp_path):
    selection = repository(tmp_path, "source")
    repo = Path(selection["repository"])
    before = git(repo, "show-ref")
    assert local_source(selection, tmp_path)["provenance"] == "local-integrated"
    assert git(repo, "show-ref") == before
    git(repo, "checkout", "-qb", "work/changed")
    (repo / "source.py").write_text("# later work\n")
    git(repo, "commit", "-qam", "later")
    with pytest.raises(ReleaseError, match="frozen commit"):
        local_source(dict(selection, commit=git(repo, "rev-parse", "HEAD")), tmp_path)
    assert git(repo, "rev-parse", "main") == selection["commit"]


@pytest.mark.parametrize("ref,match", [
    ("refs/heads/main", "protected branch main"),
    ("refs/heads/master", "protected branch master"),
    ("refs/heads/base", "protected branch base"),
    ("refs/heads/release/base", "protected branch release/base"),
    ("refs/heads/qorqut-tables", "protected branch qorqut-tables"),
    ("refs/heads/bazarlyk-history", "protected branch bazarlyk-history"),
    ("release/integration", "full refs/heads/<branch> ref"),
    ("refs/remotes/origin/release/integration", "full refs/heads/<branch> ref"),
    ("refs/heads/", "not a valid Git branch ref"),
    ("refs/heads/release/..", "not a valid Git branch ref"),
    ("refs/heads/release//integration", "not a valid Git branch ref"),
    ("refs/heads/.hidden", "not a valid Git branch ref"),
    ("refs/heads/release/integration.lock", "not a valid Git branch ref"),
    ("refs/heads/release/integration.", "not a valid Git branch ref"),
    ("refs/heads/-option", "not a valid Git branch ref"),
    ("refs/heads/release integration", "not a valid Git branch ref"),
    ("refs/heads/release/integration\n", "not a valid Git branch ref"),
    (None, "full refs/heads/<branch> ref"),
])
def test_the_declared_integrated_ref_must_be_a_valid_unprotected_branch(tmp_path, ref, match):
    selection = repository(tmp_path, "source")
    before = git(selection["repository"], "show-ref")
    with pytest.raises(ReleaseError, match=match):
        local_source(dict(selection, integrated_ref=ref), tmp_path)
    assert git(selection["repository"], "show-ref") == before


def test_the_source_must_be_a_dedicated_unprotected_branch_other_than_the_target(tmp_path):
    selection = repository(tmp_path, "source")
    repo = Path(selection["repository"])
    for name in ("main", "release/base", "base"):
        git(repo, "checkout", "-q", "-B", name, selection["commit"])
        with pytest.raises(ReleaseError, match="dedicated, non-protected branch"):
            local_source(selection, tmp_path)
    git(repo, "checkout", "-q", "release/integration")
    with pytest.raises(ReleaseError, match="must differ from release/integration"):
        local_source(selection, tmp_path)
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(ReleaseError, match="Local source verification failed"):
        local_source(selection, tmp_path)


def test_any_non_protected_prefix_is_accepted(tmp_path):
    selection = repository(tmp_path, "source")
    repo = Path(selection["repository"])
    git(repo, "branch", "-m", "release/integration", "team-a/stage/integration")
    git(repo, "checkout", "-q", "-B", "solo", selection["commit"])
    result = local_source(dict(selection, integrated_ref="refs/heads/team-a/stage/integration"), tmp_path)
    assert (result["branch"], result["provenance"]) == ("refs/heads/solo", "local-integrated")


def test_previously_integrated_exact_source_can_be_released_after_later_work(tmp_path):
    selection = repository(tmp_path, "source")
    repo = Path(selection["repository"])
    git(repo, "branch", "work/release-pin")
    git(repo, "checkout", "-q", "release/integration")
    (repo / "later.md").write_text("Unrelated later documentation")
    git(repo, "add", "later.md")
    git(repo, "commit", "-qm", "later integrated work")
    git(repo, "checkout", "-q", "work/release-pin")
    before = git(repo, "show-ref")
    assert local_source(selection, tmp_path)["commit"] == selection["commit"]
    assert git(repo, "show-ref") == before


def test_workspace_root_is_validated_and_must_exist(tmp_path):
    selection = repository(tmp_path, "source")
    link = tmp_path / "workspace-link"
    link.symlink_to(tmp_path, target_is_directory=True)
    for root, match in ((Path("/"), "filesystem root"), (Path("relative"), "absolute and normalized"),
                        (tmp_path / ".." / tmp_path.name, "absolute and normalized"),
                        (link, "symbolic links"), (tmp_path / "missing", "existing directory"),
                        (Path(selection["repository"]) / "source.py", "existing directory")):
        with pytest.raises(ReleaseError, match=match):
            local_source(selection, root)


def test_repository_and_git_metadata_must_stay_inside_the_explicit_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    selection = repository(workspace, "source")
    assert local_source(selection, workspace)["provenance"] == "local-integrated"
    nested = workspace / "source" / "nested"
    nested.mkdir()
    with pytest.raises(ReleaseError, match="inside the selected independent workspace"):
        local_source(selection, nested)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ReleaseError, match="inside the selected independent workspace"):
        local_source(selection, other)
    # An object store redirected out of the workspace is refused before any Git command runs.
    objects = Path(selection["repository"]) / ".git/objects"
    outside = tmp_path / "outside-objects"
    objects.rename(outside)
    objects.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReleaseError, match="symbolic links"):
        local_source(selection, workspace)


def test_filesystem_root_and_symlink_paths_are_refused_before_io(tmp_path):
    with pytest.raises(ReleaseError, match="filesystem root"):
        checked_path("/")
    with pytest.raises(ReleaseError, match="filesystem root"):
        checked_path(tmp_path, Path("/"))
    with pytest.raises(ReleaseError, match="absolute and normalized"):
        checked_path(tmp_path, "relative")
    with pytest.raises(ReleaseError, match="absolute and normalized"):
        checked_path(7)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "indirect"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ReleaseError, match="symbolic links"):
        checked_path(link / "nonexistent")
    with pytest.raises(ReleaseError, match="symbolic links"):
        checked_path(real / "nonexistent", link)
    assert checked_path(real / "child", real) == real / "child"
    assert checked_path(real, real) == real
    with pytest.raises(ReleaseError, match="inside the selected independent workspace"):
        checked_path(tmp_path / "elsewhere", real)
    repo = repository(tmp_path, "source")
    alternates = Path(repo["repository"]) / ".git/objects/info/alternates"
    alternates.write_text(str(tmp_path / "other-objects") + "\n")
    with pytest.raises(ReleaseError, match="independent object"):
        local_source(repo, tmp_path)
