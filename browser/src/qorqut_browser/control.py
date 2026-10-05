"""One process-safe admission gate for future direct and forwarded transports."""

from uuid import UUID, uuid4

from .contracts import (
    BrowserError, Command, ControlStatus, EffectResult, Receipt, Reconcile, canonical, digest,
)
from .state import Store


class NotStarted(BrowserError):
    """Executor positively knows no browser operation was dispatched."""


class Gate:
    """The trusted composition supplies executor/readiness, never a tool payload.

    Call startup once for a service incarnation. All entrypoints use this gate;
    child workers may inherit it. An older service token cannot admit new work.
    """

    def __init__(self, store: Store, executor, *, readiness=None):
        self.store = store
        self.executor = executor
        if readiness is None:
            from .supervisor import check_ready
            readiness = check_ready
        self.readiness = readiness
        self._token = None

    def _service(self, db):
        row = db.execute("SELECT service_token FROM browser_state WHERE singleton=1").fetchone()
        if self._token is None or row[0] != self._token:
            raise BrowserError("not_ready", "This service incarnation has not reconciled startup")

    @staticmethod
    def _same(saved, proposed):
        if (saved.kind, saved.binding, saved.actor_id, saved.payload_hash) != (
                proposed.kind, proposed.binding, proposed.actor_id, proposed.payload_hash):
            raise BrowserError("idempotency_conflict", "Command UUID already has a different actor, binding or payload")

    def _orphans(self, db):
        # Only under the effect lock: a released process lock is not evidence of
        # external completion. Preserve the intent as an unresolved effect.
        db.execute("UPDATE commands SET outcome='uncertain', error='interrupted_effect' "
                   "WHERE kind='browser' AND outcome='intent'")

    def startup(self, *, timeout=0) -> ControlStatus:
        self._token = str(uuid4())
        with self.store.transaction() as db:
            current = Store.status_in(db)
            mode = "human" if current.mode == "human" else "reconciling"
            Store.set_status(db, ControlStatus(binding=current.binding, mode=mode, ready=False),
                             evidence="Service startup requires explicit application reconciliation")
            db.execute("UPDATE browser_state SET service_token=? WHERE singleton=1", (self._token,))
            db.execute("UPDATE commands SET outcome='uncertain', error='interrupted_control', settled=1 "
                       "WHERE kind='control' AND outcome='pending'")
        try:
            with self.store.lock(timeout):
                with self.store.transaction() as db:
                    self._service(db)
                    self._orphans(db)
        except BrowserError as error:
            if error.code != "not_ready":
                raise
        return self.store.status()

    def execute(self, command: Command, *, actor_id: UUID, timeout=5) -> Receipt:
        actor_id = UUID(str(actor_id))
        proposed = Receipt(command_id=command.command_id, kind="browser", binding=command.binding,
                           actor_id=actor_id, payload_hash=digest(command.action), outcome="intent")
        with self.store.connection() as db:
            saved = Store.get_receipt(db, command.command_id)
            if saved:
                self._same(saved, proposed)
                if saved.outcome != "intent":
                    return saved
        with self.store.lock(timeout) as lock_fd:
            with self.store.transaction() as db:
                self._service(db)
                saved = Store.get_receipt(db, command.command_id)
                if saved:
                    self._same(saved, proposed)
                    if saved.outcome == "intent":
                        self._orphans(db)
                        self._hold(db, "Unconfirmed effect cannot be replayed")
                    return Store.get_receipt(db, command.command_id)
                self._admitted(db, command)
            # Slow observations/effects never hold a database transaction. The
            # transaction below rechecks admission after readiness inspection.
            self.readiness(self.store.config)
            with self.store.transaction() as db:
                self._service(db)
                self._admitted(db, command)
                Store.insert(db, proposed)
            try:
                result = EffectResult.model_validate(self.executor(command, lock_fd))
                receipt = proposed.model_copy(update={"outcome": "completed", "result": result})
                settled = True
            except NotStarted as error:
                receipt = proposed.model_copy(update={"outcome": "failed", "error": error.code})
                settled = True
            except Exception:
                # A failed click/helper/receipt response may have reached Chrome.
                # Provider prose may contain private page data; store a stable code.
                receipt = proposed.model_copy(update={"outcome": "uncertain", "error": "uncertain_effect"})
                settled = False
            with self.store.transaction() as db:
                Store.finish(db, receipt, settled=settled)
                if not settled:
                    self._hold(db, "Effect outcome requires explicit reconciliation")
            return receipt

    def _admitted(self, db, command):
        current = Store.status_in(db)
        if (current.binding != command.binding
                or command.binding.identity != self.store.config.binding.identity):
            raise BrowserError("target_changed", "Browser instance or control epoch changed")
        if current.mode != "automation" or not current.ready:
            raise BrowserError("not_ready", "Automated browser admission is closed")
        if db.execute("SELECT 1 FROM commands WHERE kind='browser' AND settled=0 LIMIT 1").fetchone():
            raise BrowserError("uncertain_effect", "An earlier browser effect is unresolved")

    def _hold(self, db, reason):
        current = Store.status_in(db)
        mode = "human" if current.mode == "human" else "reconciling"
        Store.set_status(db, ControlStatus(binding=current.binding, mode=mode, ready=False), evidence=reason)

    def reconcile(self, request: Reconcile, *, actor_id: UUID, timeout=5) -> Receipt:
        """Apply an explicit application direction. This does not create a request,
        Done, Cancel, message or viewer and is not a model-facing tool.

        Close admission and advance the epoch *before* waiting for the process
        lock. A timeout/unknown participant remains pending and cannot claim ready.
        """
        actor_id = UUID(str(actor_id))
        proposed = Receipt(command_id=request.command_id, kind="control", binding=request.expected,
                           actor_id=actor_id, payload_hash=digest(request), outcome="pending")
        with self.store.transaction() as db:
            self._service(db)
            saved = Store.get_receipt(db, request.command_id)
            if saved:
                self._same(saved, proposed)
                if saved.outcome != "pending":
                    return saved
            else:
                current = Store.status_in(db)
                if current.binding != request.expected:
                    raise BrowserError("target_changed", "Control reconciliation has a stale expected binding")
                if request.desired.identity != self.store.config.binding.identity:
                    raise BrowserError("target_changed", "Desired browser is not this service's configured instance")
                Store.insert(db, proposed, settled=True)
                mode = "yielding" if request.mode == "human" else "reconciling"
                Store.set_status(db, ControlStatus(binding=request.desired, mode=mode, ready=False),
                                 command_id=request.command_id, actor_id=actor_id,
                                 evidence=canonical({"reason": request.evidence,
                                                     "settled_commands": [str(i) for i in request.settled_commands]}))
            if Store.status_in(db).binding != request.desired:
                receipt = proposed.model_copy(update={"outcome": "uncertain", "error": "superseded_control"})
                Store.finish(db, receipt, settled=True)
                return receipt
        try:
            with self.store.lock(timeout):
                return self._settle_control(request, proposed)
        except BrowserError as error:
            if error.code != "not_ready":
                raise
            # Reading the receipt does not turn pending into completed. The
            # application retries this same UUID or explicitly supersedes it.
            with self.store.transaction() as db:
                saved = Store.get_receipt(db, request.command_id)
                if saved.outcome == "pending":
                    saved = saved.model_copy(update={"error": "not_ready"})
                    Store.finish(db, saved, settled=True)
                return saved

    def _settle_control(self, request, proposed):
        with self.store.connection() as db:
            saved = Store.get_receipt(db, request.command_id)
            if saved.outcome != "pending":
                return saved
        self.readiness(self.store.config)  # absent/unknown never confirms human readiness
        with self.store.transaction() as db:
            self._service(db)
            self._orphans(db)
            current = Store.status_in(db)
            if current.binding != request.desired:
                receipt = proposed.model_copy(update={"outcome": "uncertain", "error": "superseded_control"})
                Store.finish(db, receipt, settled=True)
                return receipt
            for command_id in request.settled_commands:
                row = db.execute("SELECT kind,outcome FROM commands WHERE command_id=?", (str(command_id),)).fetchone()
                if row is None or row["kind"] != "browser" or row["outcome"] != "uncertain":
                    raise BrowserError("invalid_argument", "Settlement requires an existing uncertain browser receipt")
                db.execute("UPDATE commands SET settled=1 WHERE command_id=?", (str(command_id),))
            if db.execute("SELECT 1 FROM commands WHERE kind='browser' AND settled=0 LIMIT 1").fetchone():
                pending = proposed.model_copy(update={"error": "uncertain_effect"})
                Store.finish(db, pending, settled=True)
                return pending
            Store.set_status(db, ControlStatus(binding=request.desired, mode=request.mode, ready=True),
                             command_id=request.command_id, actor_id=proposed.actor_id,
                             evidence=canonical({"reason": request.evidence,
                                                 "settled_commands": [str(i) for i in request.settled_commands]}))
            receipt = proposed.model_copy(update={"outcome": "completed", "result": EffectResult(text=request.mode)})
            Store.finish(db, receipt, settled=True)
            return receipt
