"""The public ``AgenticLayer`` facade: explicit ``initialize`` and ``open``."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from .authoring import Authoring
from .catalog_api import CatalogReader
from .changes import Changes
from .core import LayerContext
from .errors import InvalidRequestError, StoreError
from .history import GitRunner, History
from .store import Store
from .tools import Tools
from .util import Clock, iso, new_id, utc_now


def resolve_git(executable: str | os.PathLike[str] | None) -> str | None:
    """An explicit absolute path, or ``git`` found on PATH once (``None`` if absent)."""
    if executable is None:
        return shutil.which("git")
    path = os.fspath(executable)
    if not os.path.isabs(path):
        raise InvalidRequestError("git_executable must be an absolute path", details={"field": "git_executable"})
    return path


def _real(path: str | os.PathLike[str], field: str) -> Path:
    if path is None or os.fspath(path) == "":
        raise InvalidRequestError(f"{field} is required", details={"field": field})
    return Path(os.path.realpath(os.fspath(path)))


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


class AgenticLayer:
    """One opened authoring root plus its runtime state.

    Components: ``catalog``, ``authoring``, ``history``, ``changes``, ``tools``
    (and ``workflows``). ``runtimes`` maps a tool runtime alias to a runtime
    object (see :mod:`qorqut_agentic.runtime`); none is registered by default. Importing this module does nothing; nothing is created
    unless :meth:`initialize` is called with explicit paths.
    """

    def __init__(self, ctx: LayerContext) -> None:
        self._ctx = ctx
        self.root = ctx.root
        self.state = ctx.store.state_dir
        self.changes = Changes(ctx)
        self.authoring = Authoring(ctx, self.changes)
        self.history = History(ctx, self.changes, self.authoring)
        self.catalog = CatalogReader(ctx)
        self.tools = Tools(ctx)
        self._workflows = None

    @property
    def workflows(self):
        """Load the persistent runner only when requested."""
        if self._workflows is None:
            from .workflows import Workflows

            self._workflows = Workflows(self._ctx, self.tools)
        return self._workflows

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def initialize(
        cls,
        root: str | os.PathLike[str],
        state: str | os.PathLike[str],
        *,
        git_executable: str | os.PathLike[str] | None = None,
        git_user: Mapping[str, str] | None = None,
        runtimes: Mapping[str, Any] | None = None,
        clock: Clock | None = None,
        attribution_source: str = "caller_supplied",
    ) -> "AgenticLayer":
        """Create the authoring repository (if needed) and runtime state at explicit paths.

        ``root`` must be missing, an empty directory, or the top level of an
        existing Git working tree; ``state`` must not already be initialized and
        must lie outside ``root``. ``git_user`` (``{"name", "email"}``) is written to
        the repository's local config when given.
        """
        root_path, state_path = _real(root, "root"), _real(state, "state")
        if _inside(state_path, root_path):
            raise StoreError("the state path must be outside the authoring root", code="invalid_state_path",
                             details={"root": str(root_path), "state": str(state_path)})
        if _inside(root_path, state_path):
            raise StoreError("the authoring root must be outside the state path", code="invalid_root",
                             details={"root": str(root_path), "state": str(state_path)})
        if git_user is not None and (
            not isinstance(git_user, Mapping) or set(git_user) != {"name", "email"}
            or not all(isinstance(v, str) and v for v in git_user.values())
        ):
            raise InvalidRequestError('git_user must be {"name": ..., "email": ...}', details={"field": "git_user"})
        git = resolve_git(git_executable)
        store = Store(state_path)
        if store.exists():
            raise StoreError(f"{state_path} already holds an initialized store", code="already_initialized",
                             details={"state": str(state_path)})
        if state_path.exists() and (not state_path.is_dir() or any(
            child.name not in ("state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm", "authoring.lock", "scratch")
            for child in state_path.iterdir()
        )):
            raise StoreError("the state path exists and is not an empty or partially initialized state directory",
                             code="invalid_state_path", details={"state": str(state_path)})

        home = state_path / "scratch" / "git-home"
        has_git_dir = (root_path / ".git").exists()
        if root_path.exists():
            if not root_path.is_dir():
                raise StoreError("the authoring root is not a directory", code="invalid_root", details={"root": str(root_path)})
            if not has_git_dir and any(root_path.iterdir()):
                raise StoreError(
                    "the authoring root is a non-empty directory that is not a Git working tree; "
                    "use an empty directory or run git init first",
                    code="invalid_root", details={"root": str(root_path)},
                )
        runner = GitRunner(git, root_path, home)
        if has_git_dir:
            top = runner.text(["rev-parse", "--show-toplevel"]).strip()
            if Path(os.path.realpath(top)) != root_path:
                raise StoreError("the authoring root is inside a Git working tree but is not its top level",
                                 code="invalid_root", details={"root": str(root_path), "toplevel": top})
        else:
            root_path.mkdir(parents=True, exist_ok=True)
            runner.run(["init", "-q", "-b", "agentic"])
        if git_user is not None:
            runner.run(["config", "--local", "user.name", git_user["name"]])
            runner.run(["config", "--local", "user.email", git_user["email"]])

        clock_fn = clock or utc_now
        store.create(str(root_path), iso(clock_fn()), new_id("inst"))
        return cls._build(root_path, store, git, runtimes, clock, attribution_source)

    @classmethod
    def open(
        cls,
        root: str | os.PathLike[str],
        state: str | os.PathLike[str],
        *,
        git_executable: str | os.PathLike[str] | None = None,
        runtimes: Mapping[str, Any] | None = None,
        clock: Clock | None = None,
        attribution_source: str = "caller_supplied",
        rebind_root: bool = False,
    ) -> "AgenticLayer":
        """Open an existing store. Never creates anything; a missing store is an error.

        Opening does not repair: if an interrupted change exists,
        :attr:`recovery_required` is true and authoring mutations raise
        ``recovery_required`` until :meth:`recover` succeeds.
        """
        root_path, state_path = _real(root, "root"), _real(state, "state")
        if not root_path.is_dir():
            raise StoreError("the authoring root does not exist or is not a directory", code="invalid_root",
                             details={"root": str(root_path)})
        if _inside(state_path, root_path) or _inside(root_path, state_path):
            raise StoreError("authoring root and runtime state must be separate", code="invalid_state_path")
        store = Store(state_path)
        store.verify(str(root_path), rebind_root=rebind_root)
        return cls._build(root_path, store, resolve_git(git_executable), runtimes, clock, attribution_source)

    @classmethod
    def _build(cls, root: Path, store: Store, git: str | None, runtimes: Mapping[str, Any] | None,
               clock: Clock | None, attribution_source: str) -> "AgenticLayer":
        ctx = LayerContext(root=root, store=store, git_executable=git, runtimes=runtimes or {}, clock=clock,
                           attribution_source=attribution_source)
        return cls(ctx)

    # ----------------------------------------------------------------- recovery
    @property
    def recovery_required(self) -> bool:
        return bool(self._ctx.pending_change_ids())

    def recovery_status(self) -> dict[str, Any]:
        """Pending interrupted changes and whether ``recover`` can complete them."""
        return self.authoring.recovery_status()

    def recover(self) -> dict[str, Any]:
        """Complete interrupted changes whose files still match their recorded hashes."""
        return self.authoring.recover()
