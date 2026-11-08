"""Explicit provisioning of the operations-root Git repository of one fresh instance. Never repair, adoption or startup work.

The retained table-history publisher (``refs/heads/qorqut-tables``) and the nightly history publisher
(``refs/heads/qorqut-history``) both publish through the Git repository at ``operations/``. The table publisher starts from
an empty index; the nightly publisher starts from its own ref or, on its first run, from ``HEAD``, which must therefore name a
commit. ``provision_history_repository`` is called by the fresh-instance initializer, and by nothing else, to give a new
instance exactly that starting state:

* a private repository at ``operations/.git`` whose checked-out branch is the neutral ``qorqut-instance`` (never either
  publisher's history branch), with no remote, no hooks, no template files and no signing;
* ONE bootstrap commit on that branch whose tree is Git's empty tree, with no parent. It is built explicitly (``mktree`` of
  nothing, ``commit-tree``, a compare-and-swap ``update-ref``): no instance content is scanned, staged or hashed, and no index is
  written.

Only a repository this call creates receives the bootstrap commit. Anything already at ``operations/.git`` (a repository with or
without commits, a gitfile, a symlink, any other entry) is left exactly as found and reported as not created; this module never
inspects, repairs or completes it. Normal import, runtime construction, serving and table reads never call it.

Every Git command runs against the explicit ``operations/.git`` directory with an allowlisted environment (no inherited ``GIT_*``
variable, no system or global configuration, no prompt) and with the operations root's parent as the Git ceiling directory, so a
missing or unusable ``.git`` can never make Git fall back to an ancestor repository. A failure after ``git init`` raises and leaves
what exists, for inspection; the initializer reports that as an incomplete new instance. Nothing is retried or removed."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess

from qorqut_operations.contracts import DomainError, utc_now

BRANCH = "qorqut-instance"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"       # Git's empty tree under the pinned sha1 object format
IDENTITY = ("Qorqut", "qorqut@localhost")                      # the table publisher's machine identity; no personal configuration
MESSAGE = "Initialize the Qorqut instance repository\n\nEmpty bootstrap commit: no instance content is recorded.\n"
TIMEOUT = 30
OBJECT_ID = re.compile(r"[0-9a-f]{40}")
LOCAL_ONLY = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "commit.gpgSign=false",
              "-c", "tag.gpgSign=false", "-c", "protocol.allow=never")


@dataclass(frozen=True)
class HistoryRepository:
    """``created`` is False when ``operations/.git`` already existed: nothing was read or changed, and ``commit`` is None."""

    path: Path
    created: bool
    branch: str | None = None
    commit: str | None = None


def _refuse(message: str) -> DomainError:
    return DomainError("invalid_configuration", message)


def operations_root(value) -> Path:
    """The explicit operations root: absolute, without traversal or symlinks, an existing owner-private directory."""
    if not isinstance(value, (str, os.PathLike)):
        raise _refuse("The operations root must be a path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in str(path) or path == Path(path.anchor):
        raise _refuse("The operations root must be an absolute path without traversal, and not the filesystem root")
    if os.path.realpath(path) != str(path):
        raise _refuse("The operations root and its ancestors must not be symlinks")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _refuse("The operations root is missing") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise _refuse("The operations root must be a directory owned by the service user with mode 0700")
    return path


def _failed(message: str, **details) -> DomainError:
    return DomainError("history_repository_failed", message, details=details or None)


class _Git:
    def __init__(self, root: Path, runner, date: str):
        self.root, self.runner, self.date = root, runner, date
        self.git_dir = root / ".git"

    def environment(self, *, repository: bool) -> dict:
        # Only an allowlist survives: an inherited GIT_* variable could redirect the repository, index, identity or configuration.
        env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
        env.update(LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, XDG_CONFIG_HOME=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_CEILING_DIRECTORIES=str(self.root.parent),
                   GIT_AUTHOR_NAME=IDENTITY[0], GIT_COMMITTER_NAME=IDENTITY[0],
                   GIT_AUTHOR_EMAIL=IDENTITY[1], GIT_COMMITTER_EMAIL=IDENTITY[1],
                   GIT_AUTHOR_DATE=self.date, GIT_COMMITTER_DATE=self.date)
        if repository:
            env["GIT_DIR"] = str(self.git_dir)
        return env

    def __call__(self, *arguments, data=b"", repository=True, allowed=(0,)) -> str:
        try:
            result = self.runner(["git", *LOCAL_ONLY, *arguments], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=self.environment(repository=repository), cwd=str(self.root), timeout=TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _failed("Git did not complete the history repository setup", command=arguments[0]) from exc
        if result.returncode not in allowed:
            raise _failed("Git failed the history repository setup", command=arguments[0], returncode=result.returncode)
        return result.stdout.decode("utf-8", "replace").strip()

    def oid(self, *arguments, **options) -> str:
        value = self(*arguments, **options)
        if not OBJECT_ID.fullmatch(value):
            raise _failed("Git returned an invalid object identity", command=arguments[0])
        return value


def provision_history_repository(operations, *, runner=None, created_at=None) -> HistoryRepository:
    """Create the private repository at ``operations/.git`` with one empty bootstrap commit, unless ``.git`` already exists.

    ``runner`` and ``created_at`` exist for tests (a ``subprocess.run`` substitute; the commit date, a UTC ISO timestamp)."""
    root = operations_root(operations)
    git_dir = root / ".git"
    if os.path.lexists(git_dir):
        return HistoryRepository(root, created=False)
    git = _Git(root, runner or subprocess.run, created_at or utc_now())
    # The caller's umask keeps the new repository owner-only; this makes it independent of the caller.
    previous_umask = os.umask(0o077)
    try:
        git("init", "--quiet", "--template=", "--object-format=sha1", f"--initial-branch={BRANCH}", str(root), repository=False)
        tree = git.oid("mktree", data=b"")
        if tree != EMPTY_TREE:
            raise _failed("The bootstrap tree is not Git's empty tree")
        commit = git.oid("commit-tree", tree, "-m", MESSAGE)
        git("update-ref", f"refs/heads/{BRANCH}", commit, "0" * len(commit))
        _verify(git, commit)
    finally:
        os.umask(previous_umask)
    return HistoryRepository(root, created=True, branch=BRANCH, commit=commit)


def _verify(git: _Git, commit: str) -> None:
    """The new repository is exactly the intended one: its own top level, the neutral branch, one parentless empty commit."""
    toplevel = git("rev-parse", "--show-toplevel", "--absolute-git-dir", repository=False).split("\n")
    if toplevel != [str(git.root), str(git.git_dir)]:
        raise _failed("The new repository is not the operations root's own repository")
    if git("symbolic-ref", "HEAD") != f"refs/heads/{BRANCH}" or git("for-each-ref", "--format=%(refname)") != f"refs/heads/{BRANCH}":
        raise _failed("The new repository does not have exactly the neutral branch checked out")
    if git.oid("rev-parse", "--verify", "HEAD^{commit}") != commit or git.oid("rev-parse", "HEAD^{tree}") != EMPTY_TREE:
        raise _failed("HEAD is not the empty bootstrap commit")
    if git("rev-list", "--parents", "HEAD") != commit or git("ls-tree", "-r", "HEAD") != "":
        raise _failed("The bootstrap history is not a single parentless empty commit")
    if "gpgsig" in git("cat-file", "commit", commit):
        raise _failed("The bootstrap commit must not be signed")
    configuration = git("config", "--local", "--list", "--name-only")
    if any(key.startswith(("remote.", "include", "branch.", "user.", "core.worktree")) for key in configuration.split("\n")):
        raise _failed("The new repository has unexpected local configuration")
    if os.path.lexists(git.git_dir / "index") or os.path.lexists(git.git_dir / "hooks"):
        raise _failed("The new repository has an index or hooks")
    for path in (git.git_dir, *git.git_dir.rglob("*")):
        if not path.is_symlink() and path.lstat().st_mode & 0o077:
            raise _failed("The new repository is not owner-private")
