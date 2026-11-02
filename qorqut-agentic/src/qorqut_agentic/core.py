"""Shared context handed to every component of one opened layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .catalog import Catalog, load_catalog
from .errors import RecoveryRequiredError
from .files import DiskSource, SafeTree
from .store import Store
from .util import Clock, iso, utc_now


class LayerContext:
    def __init__(
        self,
        *,
        root: Path,
        store: Store,
        git_executable: str | None,
        runtimes: Mapping[str, Any],
        clock: Clock | None,
        attribution_source: str,
        hooks: dict[str, Callable[..., None]] | None = None,
    ) -> None:
        self.root = root
        self.tree = SafeTree(root)
        self.store = store
        self.git_executable = git_executable
        self.runtimes: dict[str, Any] = dict(runtimes)
        self.clock = clock or utc_now
        self.attribution_source = attribution_source
        # Fault-injection seam for tests: name -> callable(**context).
        self.hooks: dict[str, Callable[..., None]] = hooks if hooks is not None else {}

    def now(self) -> str:
        return iso(self.clock())

    def hook(self, name: str, **context: Any) -> None:
        callback = self.hooks.get(name)
        if callback is not None:
            callback(**context)

    # -- catalog ---------------------------------------------------------------
    def load_catalog_locked(self) -> Catalog:
        """Load while the caller already holds the authoring lock."""
        return load_catalog(DiskSource(self.tree))

    # -- recovery state -----------------------------------------------------------
    def pending_change_ids(self) -> list[str]:
        with self.store.reader() as conn:
            return [row["change_id"] for row in conn.execute("SELECT change_id FROM pending_changes ORDER BY created_at")]

    def require_no_recovery(self) -> None:
        pending = self.pending_change_ids()
        if pending:
            raise RecoveryRequiredError(
                "an interrupted change must be recovered before further authoring changes",
                details={"pending_change_ids": pending},
            )
