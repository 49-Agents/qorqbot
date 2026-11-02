"""One long-poll pass: recover stored updates, poll, persist the batch with its cursor, then process it."""

from __future__ import annotations

from qorqut_operations.contracts import DomainError
from . import store
from .api import TelegramApiError
from .bridge import FAIL_CLOSED
from .normalize import normalize_update

OUTAGE_SECONDS = 20 * 3600


def _error_state(bridge, code):
    try:
        with bridge.write("poll.error") as uow:
            store.touch_state(uow, last_error_json={"code": code, "at": bridge.now()})
    except DomainError:
        pass


def poll_once(bridge):
    """Returns the seconds to wait before the next pass (0 means poll again immediately)."""
    try:
        return _poll(bridge)
    except DomainError as exc:
        # An owner, installation or binding refusal closes polling and ingestion until the operator reconfigures.
        if exc.code not in FAIL_CLOSED:
            raise
        bridge.fail_closed(exc)
        return 5.0


def _poll(bridge):
    try:
        config = bridge.current_config()
    except DomainError as exc:
        if exc.code in FAIL_CLOSED:
            raise
        return 1.0
    api, bot_id = bridge.api, config.bot_id
    try:
        # A previous run may have stored updates it did not finish: they are handled before anything new is requested.
        bridge.inbound.process_pending()
        with bridge.read() as uow:
            state = store.get_state(uow)
        offset = state["poll_offset"]
        polled_from = state["last_poll_at"]
        raw = api.get_updates(offset, bridge.poll_timeout)
    except TelegramApiError as exc:
        return _poll_failure(bridge, exc)
    except DomainError as exc:
        if exc.code in ("maintenance", "busy"):
            return 5.0
        raise
    bridge.recover_component("poller")
    items = [item for item in (normalize_update(update) for update in (raw if isinstance(raw, list) else [])) if item is not None]
    items.sort(key=lambda item: item["update_id"])
    items = bridge.inbound.prepare(items)
    now = bridge.now()
    with bridge.write("poll.store") as uow:
        store.store_batch(uow, bot_id, items, now=now, polled_at=now)
        if polled_from and store.parse(now) - store.parse(polled_from) > store.timedelta(seconds=OUTAGE_SECONDS):
            # Telegram keeps updates for about a day: a gap this long may have dropped some. Report, never guess.
            store.touch_state(uow, outage_json={"from": polled_from, "to": now, "possible_message_loss": True})
    bridge.inbound.process_pending()
    return 0.0


def _poll_failure(bridge, exc):
    if exc.kind == "rate_limited":
        bridge.degrade("poller", "api_rate_limited")
        return max(1.0, exc.retry_after or 1.0)
    if exc.kind == "refused":
        if exc.error_code == 409:
            bridge.degrade("poller", "poller_conflict")
            _error_state(bridge, "poller_conflict")
            return 30.0
        bridge.degrade("poller", f"refused_{exc.error_code}")
        _error_state(bridge, f"refused_{exc.error_code}")
        return 30.0
    bridge.degrade("poller", "api_" + exc.kind)
    return min(60.0, 2.0 + bridge.rng.random() * 3.0)
