"""The fresh-instance operations-root repository helper on disposable directories: a private repository with the neutral branch and
ONE empty bootstrap commit, never an existing repository, never an ancestor, never instance content.

Real Git throughout (``tmp_path`` only); the runner substitutes only record or break commands. No instance, service, network or
operational data is involved - the initializer and both publishers on a real fresh instance are in
``test_fresh_history_publishers.py``."""

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

from qorqut_operations.contracts import DomainError

from bazarlyk.bootstrap import history_repository as repository
from bazarlyk.bootstrap.history_repository import BRANCH, EMPTY_TREE, HistoryRepository, provision_history_repository

DATE = "2026-10-25T10:00:00Z"
# The refs the two retained publishers own; the neutral branch is neither (their modules are not imported: they pull in the host).
PUBLISHER_REFS = ("refs/heads/qorqut-tables", "refs/heads/bazarlyk-history")
# Git subcommands that would read, stage, scan or transmit instance content or touch another repository's state.
CONTENT_COMMANDS = {"add", "commit", "status", "checkout", "read-tree", "write-tree", "update-index", "hash-object", "remote", "push",
                    "fetch", "clone", "worktree", "gc", "reset", "stash", "merge", "rebase"}


def private(base, name="operations"):
    """A private ``operations`` directory (0700) below ``base`` (made when absent), as the fresh initializer leaves it."""
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = base / name
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def git(root, *arguments, env=None, check=True):
    """The test's own Git, independent of the helper: no ambient or global configuration."""
    clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    clean.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    result = subprocess.run(["git", "-C", str(root), *arguments], env=clean, capture_output=True, text=True)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def fingerprint(path):
    """Every entry below (and including) ``path`` with type, mode and content hash: a change of any kind shows."""
    entries = {}
    for item in [Path(path), *sorted(Path(path).rglob("*"))]:
        info = item.lstat()
        body = hashlib.sha256(item.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else (os.readlink(item) if item.is_symlink() else "")
        entries[str(item.relative_to(Path(path).parent))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), body)
    return entries


def entry(path):
    """What is at ``path`` without following it: a link's target, a file's bytes or a directory's whole fingerprint."""
    return os.readlink(path) if path.is_symlink() else fingerprint(path)


class Recorder:
    """``subprocess.run`` that records each command; ``fail`` makes one subcommand exit non-zero without running it."""

    def __init__(self, fail=None, raises=None):
        self.commands, self.fail, self.raises = [], fail, raises

    def __call__(self, command, **options):
        subcommand = command[1 + len(repository.LOCAL_ONLY)]
        self.commands.append((subcommand, command, options))
        if subcommand == self.raises:
            raise OSError("simulated Git failure")
        if subcommand == self.fail:
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"simulated failure")
        return subprocess.run(command, **options)


def test_a_fresh_repository_has_the_neutral_branch_checked_out_with_one_empty_parentless_commit(tmp_path):
    root = private(tmp_path)
    (root / "PRIVATE-SENTINEL.txt").write_text("instance content that must never be read into Git\n")
    (root / ".runtime").mkdir(mode=0o700)
    (root / ".runtime" / "operations.sqlite3").write_bytes(b"not captured")
    before = fingerprint(root)
    runner = Recorder()
    result = provision_history_repository(root, runner=runner, created_at=DATE)
    assert result.created and result.path == root and result.branch == BRANCH == "qorqut-instance" and result.branch not in PUBLISHER_REFS
    assert f"refs/heads/{BRANCH}" not in PUBLISHER_REFS

    # HEAD is the neutral branch and names the bootstrap commit - never unborn.
    assert git(root, "symbolic-ref", "HEAD") == "refs/heads/" + BRANCH
    assert git(root, "rev-parse", "--verify", "HEAD^{commit}") == result.commit
    assert git(root, "for-each-ref", "--format=%(refname)") == "refs/heads/" + BRANCH
    # One commit, no parent, and its tree is Git's empty tree: no entry at all.
    assert git(root, "rev-list", "--count", "HEAD") == "1" and git(root, "rev-list", "--parents", "HEAD") == result.commit
    assert git(root, "rev-parse", "HEAD^{tree}") == EMPTY_TREE and git(root, "ls-tree", "-r", "-t", "HEAD") == ""
    assert git(root, "cat-file", "-t", EMPTY_TREE) == "tree"
    # Local machine identity from the fixed date, unsigned.
    assert git(root, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%aI|%s") == "Qorqut <qorqut@localhost>|Qorqut <qorqut@localhost>|2026-10-25T10:00:00+00:00|Initialize the Qorqut instance repository"
    assert "gpgsig" not in git(root, "cat-file", "commit", result.commit)
    # Its own top level (never an ancestor), no remote, no include/user/signing/hook configuration, no hook files, no index.
    assert git(root, "rev-parse", "--show-toplevel") == str(root) and git(root, "rev-parse", "--absolute-git-dir") == str(root / ".git")
    assert git(root, "remote") == "" and git(root, "config", "--local", "--list", "--name-only").split("\n") == [
        "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates"]
    assert not (root / ".git" / "hooks").exists() and not (root / ".git" / "index").exists() and not (root / ".git" / "info").exists()
    assert not (root / ".git" / "packed-refs").exists() and not (root / ".git" / "objects" / "info" / "alternates").exists()
    # Private: owner-only everywhere, whatever the caller's umask was.
    assert [str(path) for path in (root / ".git", *(root / ".git").rglob("*")) if not path.is_symlink() and path.lstat().st_mode & 0o077] == []
    # Instance content was neither read nor staged: the sentinels are untracked and untouched, and Git ran only plumbing for it.
    assert git(root, "status", "--porcelain", "--untracked-files=all").split("\n") == ["?? .runtime/operations.sqlite3", "?? PRIVATE-SENTINEL.txt"]
    assert git(root, "ls-files") == ""
    after = fingerprint(root)
    assert {name: entry for name, entry in after.items() if ".git" not in Path(name).parts} == before
    assert [subcommand for subcommand, *_ in runner.commands if subcommand in CONTENT_COMMANDS] == []
    assert [subcommand for subcommand, *_ in runner.commands][:4] == ["init", "mktree", "commit-tree", "update-ref"]


def test_the_same_inputs_give_the_same_commit_whatever_the_ambient_git_environment(tmp_path, monkeypatch):
    clean = provision_history_repository(private(tmp_path / "clean"), created_at=DATE)

    # A hostile environment: global configuration that would sign, set an identity and a default branch and install hooks, a
    # redirected repository, index, work tree and author, and configuration injected through the environment.
    home = tmp_path / "hostile-home"
    home.mkdir()
    marker, hook_template = tmp_path / "gpg-ran", tmp_path / "template"
    (hook_template / "hooks").mkdir(parents=True)
    (hook_template / "hooks" / "reference-transaction").write_text(f"#!/bin/sh\necho hook > {marker}.hook\n")
    (hook_template / "hooks" / "reference-transaction").chmod(0o755)
    program = tmp_path / "gpg-stub"
    program.write_text(f"#!/bin/sh\necho signing > {marker}\nexit 1\n")
    program.chmod(0o755)
    configuration = home / "gitconfig"
    configuration.write_text(f"[user]\n name = Somebody Else\n email = else@example.test\n signingkey = ABC\n[commit]\n gpgsign = true\n"
                             f"[tag]\n gpgsign = true\n[gpg]\n program = {program}\n[init]\n defaultBranch = main\n templateDir = {hook_template}\n"
                             f"[core]\n hooksPath = {hook_template}/hooks\n sharedRepository = group\n logAllRefUpdates = false\n[remote \"origin\"]\n url = https://example.test/x.git\n")
    (home / ".gitconfig").write_text(configuration.read_text())
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    for key, value in {"HOME": str(home), "XDG_CONFIG_HOME": str(home), "GIT_CONFIG_GLOBAL": str(configuration),
                       "GIT_DIR": str(decoy / "elsewhere.git"), "GIT_INDEX_FILE": str(decoy / "index"), "GIT_WORK_TREE": str(decoy),
                       "GIT_AUTHOR_NAME": "Somebody Else", "GIT_COMMITTER_EMAIL": "else@example.test", "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "true",
                       "GIT_TEMPLATE_DIR": str(hook_template), "GIT_OBJECT_DIRECTORY": str(decoy / "objects"),
                       "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(decoy)}.items():
        monkeypatch.setenv(key, value)
    configuration_before = configuration.read_bytes()
    hostile = provision_history_repository(private(tmp_path / "hostile"), created_at=DATE)

    assert hostile.commit == clean.commit                      # same tree, identity, date, message and parentless: same object
    assert not marker.exists() and not Path(str(marker) + ".hook").exists()
    root = hostile.path
    assert git(root, "symbolic-ref", "HEAD") == "refs/heads/" + BRANCH
    assert not (root / ".git" / "hooks").exists() and not (root / ".git" / "index").exists()
    assert (root / ".git" / "logs" / "HEAD").is_file() and stat.S_IMODE((root / ".git").stat().st_mode) == 0o700       # neither setting applied
    assert sorted(path.name for path in decoy.iterdir()) == []                # nothing was redirected into the decoy
    assert configuration.read_bytes() == configuration_before and (home / ".gitconfig").read_text() == configuration_before.decode()   # no config written
    assert "remote" not in (root / ".git" / "config").read_text() and "gpgsign" not in (root / ".git" / "config").read_text()


def test_an_ancestor_repository_is_never_used_or_changed(tmp_path):
    ancestor = tmp_path / "source-checkout"
    ancestor.mkdir()
    git(ancestor, "init", "--quiet", "--initial-branch=trunk")
    (ancestor / "tracked.txt").write_text("tracked\n")
    git(ancestor, "add", "tracked.txt")
    git(ancestor, "-c", "user.name=Owner", "-c", "user.email=owner@example.test", "commit", "--quiet", "-m", "owner work")
    (ancestor / "staged.txt").write_text("staged\n")
    git(ancestor, "add", "staged.txt")
    snapshot = fingerprint(ancestor / ".git")
    head = git(ancestor, "rev-parse", "HEAD")
    root = private(ancestor / "instances" / "fresh")

    result = provision_history_repository(root, created_at=DATE)
    assert result.created and git(root, "rev-parse", "--show-toplevel") == str(root)
    assert git(ancestor, "rev-parse", "HEAD") == head and git(ancestor, "for-each-ref", "--format=%(refname)") == "refs/heads/trunk"
    assert {name: entry for name, entry in fingerprint(ancestor / ".git").items()} == snapshot       # objects, index, refs, logs: nothing

    # Even if Git's own initialization were skipped, every later command names operations/.git explicitly: it fails, and the
    # ancestor is still not used (no fallback, no ref, no object written there).
    class SkipsInit(Recorder):
        def __call__(self, command, **options):
            if "init" in command:
                self.commands.append(("init", command, options))
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
            return super().__call__(command, **options)

    second = private(ancestor / "instances", "second")
    with pytest.raises(DomainError, match="Git failed the history repository setup"):
        provision_history_repository(second, runner=SkipsInit(), created_at=DATE)
    assert not (second / ".git").exists()
    assert {name: entry for name, entry in fingerprint(ancestor / ".git").items()} == snapshot and git(ancestor, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("kind", ["empty-directory", "unborn-repository", "repository-with-history", "gitfile", "symlink-to-ancestor"])
def test_anything_already_at_dot_git_is_left_exactly_as_found(tmp_path, kind):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    git(ancestor, "init", "--quiet")
    root = private(tmp_path / "instance")
    existing = root / ".git"
    if kind == "empty-directory":
        existing.mkdir(mode=0o700)
    elif kind == "unborn-repository":
        git(root, "init", "--quiet", "--initial-branch=owner-branch")
    elif kind == "repository-with-history":
        git(root, "init", "--quiet", "--initial-branch=owner-branch")
        (root / "owner.txt").write_text("owner\n")
        git(root, "add", "owner.txt")
        git(root, "-c", "user.name=Owner", "-c", "user.email=owner@example.test", "commit", "--quiet", "-m", "owner baseline")
        git(root, "branch", "other")
    elif kind == "gitfile":
        existing.write_text(f"gitdir: {ancestor / '.git'}\n")
    else:
        existing.symlink_to(ancestor / ".git")
    (root / "keep.txt").write_text("working file\n")
    before_repository, before_working, before_ancestor = entry(existing), (root / "keep.txt").read_bytes(), fingerprint(ancestor / ".git")
    runner = Recorder(raises="init")
    if kind == "unborn-repository":
        assert git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False) == ""

    result = provision_history_repository(root, runner=runner, created_at=DATE)
    assert result == HistoryRepository(root, created=False) and result.commit is None
    assert runner.commands == []                                  # not even read: no Git command at all
    assert entry(existing) == before_repository
    assert (root / "keep.txt").read_bytes() == before_working and fingerprint(ancestor / ".git") == before_ancestor
    if kind == "unborn-repository":                                # never repaired: HEAD stays unborn on the owner's branch
        assert git(root, "symbolic-ref", "HEAD") == "refs/heads/owner-branch" and git(root, "for-each-ref") == ""
    if kind == "repository-with-history":
        assert sorted(git(root, "for-each-ref", "--format=%(refname)").split("\n")) == ["refs/heads/other", "refs/heads/owner-branch"]


@pytest.mark.parametrize("case", ["relative", "traversal", "root", "missing", "file", "symlink", "ancestor-symlink", "group-readable",
                                  "not-a-path"])
def test_an_unsafe_operations_root_is_refused_before_anything_is_created(tmp_path, case):
    real = private(tmp_path / "real-parent")
    (tmp_path / "file").write_text("x")
    (tmp_path / "link").symlink_to(real)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    target = {"relative": Path("operations"), "traversal": real / ".." / real.name, "root": Path("/"), "missing": tmp_path / "absent",
              "file": tmp_path / "file", "symlink": tmp_path / "link", "ancestor-symlink": tmp_path / "link" / "child",
              "group-readable": shared, "not-a-path": 7}[case]
    runner = Recorder(raises="init")
    before = fingerprint(tmp_path)
    with pytest.raises(DomainError) as refused:
        provision_history_repository(target, runner=runner, created_at=DATE)
    assert refused.value.code == "invalid_configuration" and runner.commands == []
    assert fingerprint(tmp_path) == before and not (real / ".git").exists() and not (shared / ".git").exists()


@pytest.mark.parametrize("step", ["init", "mktree", "commit-tree", "update-ref", "rev-list"])
def test_a_failure_leaves_what_exists_and_nothing_repairs_it_later(tmp_path, step):
    root = private(tmp_path)
    with pytest.raises(DomainError) as failed:
        provision_history_repository(root, runner=Recorder(fail=step), created_at=DATE)
    assert failed.value.code == "history_repository_failed"
    if step == "init":
        assert not (root / ".git").exists()
        return
    # The new repository is incomplete (HEAD may be unborn) and is reported as such; a second call does not touch it.
    snapshot = fingerprint(root / ".git")
    again = provision_history_repository(root, created_at=DATE)
    assert again == HistoryRepository(root, created=False) and fingerprint(root / ".git") == snapshot
    if step in ("mktree", "commit-tree"):
        assert git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False) == ""        # unborn: never completed behind the failure


def test_git_that_cannot_run_is_a_setup_failure_and_the_umask_is_restored(tmp_path):
    root = private(tmp_path)
    before = os.umask(0o022)
    try:
        with pytest.raises(DomainError, match="did not complete"):
            provision_history_repository(root, runner=Recorder(raises="init"), created_at=DATE)
        assert os.umask(0o022) == 0o022
        provision_history_repository(root, created_at=DATE)                          # caller umask 022 still yields an owner-only repository
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)
    assert [str(path) for path in (root / ".git").rglob("*") if not path.is_symlink() and path.lstat().st_mode & 0o077] == []
    assert stat.S_IMODE((root / ".git").stat().st_mode) == 0o700


def test_only_the_fresh_initializer_calls_the_helper():
    """Import, runtime construction, serving, table reads and both publishers never provision or repair a repository."""
    source = Path(repository.__file__).parents[1]
    importer = re.compile(r"^\s*(?:from\s+\S*history_repository\s+import|from\s+\S+\s+import\s+[^#\n]*\bhistory_repository\b|import\s+\S*history_repository)", re.M)
    callers = sorted(path.relative_to(source).as_posix() for path in source.rglob("*.py")
                     if path != Path(repository.__file__) and importer.search(path.read_text()))
    assert callers == ["bootstrap/initialize.py"]
