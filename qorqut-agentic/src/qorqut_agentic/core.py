"""Shared context handed to every component of one opened layer."""

from __future__ import annotations

from pathlib import Path
import json
import threading
from contextlib import contextmanager
from typing import Any, Callable, Mapping

from .catalog import Catalog, load_catalog
from .errors import AgenticError, RecoveryRequiredError
from .files import DiskSource, SafeTree
from .store import Store
from .util import Clock, iso, utc_now


_CHECK_EFFECTS = threading.local()


@contextmanager
def check_effects_scope():
    prior = getattr(_CHECK_EFFECTS, "active", False)
    _CHECK_EFFECTS.active = True
    try:
        yield
    finally:
        _CHECK_EFFECTS.active = prior


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
            pending = [row["change_id"] for row in conn.execute("SELECT change_id FROM pending_changes ORDER BY created_at")]
            pending.extend(row["change_id"] for row in conn.execute(
                "SELECT change_id FROM overseer_publications WHERE stage NOT IN ('prepared','merged','complete','superseded') ORDER BY operation_id"))
            return pending

    def require_no_recovery(self, *, publications_only: bool = False) -> None:
        with self.store.reader() as conn:
            publications = [row["change_id"] for row in conn.execute(
                "SELECT change_id FROM overseer_publications WHERE stage NOT IN ('prepared','merged','complete','superseded') ORDER BY operation_id")]
        if publications:
            raise AgenticError("an interrupted publication requires recovery", code="publication_recovery_required",
                               details={"pending_change_ids": publications})
        if publications_only:
            return
        pending = self.pending_change_ids()
        if pending:
            raise RecoveryRequiredError(
                "an interrupted change must be recovered before further authoring changes",
                details={"pending_change_ids": pending},
            )

    def require_no_check_mutation(self) -> None:
        if getattr(_CHECK_EFFECTS, "active", False):
            raise AgenticError("mutations are forbidden from an application check callback", code="invalid_transition")

    def guard_overseer_admission(self, conn, *, application_id=None, delta_id=None, run_id=None, allow_uncertain_control=False) -> None:
        """Refuse changes to an application whose publication is incomplete."""
        self.require_no_check_mutation()
        if not any((application_id, delta_id, run_id)):
            return
        for row in conn.execute("""SELECT a.id,a.delta_id,a.run_id,p.change_id FROM overseer_publications p
                                    JOIN delta_applications a ON a.id=p.application_id
                                    WHERE p.stage NOT IN ('prepared','merged','complete','superseded')"""):
            if ((application_id and row['id'] == application_id) or (delta_id and row['delta_id'] == delta_id)
                or (run_id and row['run_id'] == run_id)):
                raise AgenticError('application publication requires recovery', code='publication_recovery_required',
                                   details={'pending_change_ids': [row['change_id']]})
        if allow_uncertain_control:
            return
        for row in conn.execute('SELECT result FROM overseer_operations'):
            item = json.loads(row['result'])
            if item.get('_state') != 'check_running':
                continue
            app = conn.execute('SELECT id,delta_id,run_id FROM delta_applications WHERE id=?', (item.get('application_id'),)).fetchone()
            if app and ((application_id and app['id'] == application_id) or (delta_id and app['delta_id'] == delta_id)
                        or (run_id and app['run_id'] == run_id)):
                raise AgenticError('application check is running', code='invalid_transition')
