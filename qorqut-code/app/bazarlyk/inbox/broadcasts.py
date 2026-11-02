"""Host side of the broadcasts; the definitions, scheduling and delivery are Operations' ``Broadcasts`` and ``BroadcastDispatch``.

``Broadcasts`` binds the host's global-agent facts (who is an active global, its profile, where it reaches) and its
owner-directed contact rule. ``BroadcastDispatcher`` keeps only what is the host's: the thread and its lifecycle, the
terminal input call and the authority and protected-control checks it hands to the delivery engine."""
from datetime import datetime, timezone
from threading import Event, Thread

from qorqut_operations.contracts import DomainError
from qorqut_operations.inbox import BroadcastDispatch, Broadcasts as OperationsBroadcasts
from bazarlyk.agents.contact import owner_directed_target
from bazarlyk.agents.global_scope import active_global, global_profile_data, reaches_business
from bazarlyk.management.identity import require_protected_control


class Broadcasts(OperationsBroadcasts):
    def __init__(self, inbox, *, clock=lambda: datetime.now(timezone.utc)):
        super().__init__(inbox, active_global=active_global, global_profile_data=global_profile_data,
                         reaches_business=reaches_business, owner_directed_target=owner_directed_target, clock=clock)


class BroadcastDispatcher:
    """Independent bounded delivery loop; no model calls or terminal startup."""
    def __init__(self, runtime, broadcasts, terminals=None, *, clock=lambda: datetime.now(timezone.utc), interval=2):
        self.runtime, self.broadcasts, self.terminals = runtime, broadcasts, terminals
        self.clock, self.interval = clock, interval
        self.stop_event, self.thread = Event(), None

    @property
    def engine(self):
        """The delivery engine over the current runtime, broadcasts, terminals and clock; it keeps nothing between calls."""
        return BroadcastDispatch(self.runtime.application, self.runtime.database, self.broadcasts,
            require_authority=self.runtime.require_authority, require_protected_control=require_protected_control,
            input_existing=self._input_existing, stopping=self.stop_event.is_set, clock=lambda: self.clock())

    def _input_existing(self, business_id, recipient, message, *, key):
        if not self.terminals:
            return {'state': 'unavailable', 'detail': 'Terminal backend is unavailable'}
        return self.terminals.input_existing(business_id, recipient, message, key=key)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = Thread(target=self._loop, name="bazarlyk-broadcast-dispatch", daemon=True)
        self.thread.start()

    def stop(self, timeout=5):
        self.stop_event.set()
        thread = self.thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("Broadcast dispatcher is still draining")
        self.thread = None

    @staticmethod
    def _next(timing, now):
        return BroadcastDispatch._next(timing, now)

    def tick(self):
        return self.engine.tick()

    def _tick(self):
        return self.engine._tick()

    def _delivery_context(self, payload, delivery, uow):
        return self.engine._delivery_context(payload, delivery, uow)

    def _deliver(self, broadcast_id, occurrence):
        return self.engine._deliver(broadcast_id, occurrence)

    def _loop(self):
        import logging
        logger = logging.getLogger(__name__)
        while not self.stop_event.is_set():
            try:
                self.tick()
            except DomainError as exc:
                if exc.code != 'maintenance':
                    logger.warning('Broadcast dispatcher paused this check: %s', exc.code)
            except Exception:
                logger.exception('Broadcast dispatcher could not finish a check')
            self.stop_event.wait(self.interval)
