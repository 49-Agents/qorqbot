"""What Qorqbot knows, in memory, about the owner's Claude Code and Codex accounts beyond what the clients store: whether a usage
limit was hit, and what the last account switch did.

Nothing here is saved. A restart of the service forgets it; the first failed turn after that sets a limit again, and the
clients' own stored sign-in is the truth about who is signed in. Three things live here:

* **The limit of a product.** A usage limit belongs to the account, so one agent that hit it marks the whole product (``claude`` or
  ``codex``) as limited, for every agent on it. The mark ends at its reset time, when a turn on that product succeeds after the
  limited one, or when the account is switched. ``observe_turn`` is fed by the managed runtime from every agent's last turn.
* **The last switch.** ``record_switch`` / ``update_switch`` keep the account before and after a sign-in, when it happened and how
  many agents it restarted and told to continue.
* **The accounts as Settings reads them**, kept a short time (``clients_view``), so a Chat read that has to name the account does not
  ask the clients every time (Codex's status is a process).

The text the owner sees is the dashboard's. Times are ISO 8601 in UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

PROVIDERS = ("claude", "codex")
CLIENTS_TTL = 30.0           # seconds Settings' view of the clients is reused by Chat
_lock = threading.RLock()
_limits: dict = {}           # provider -> {"turn_at": datetime, "resets_at": iso | None, "resets_text": str | None}
_ignore_before: dict = {}    # provider -> datetime: a turn that finished before it says nothing about the current account
_switches: dict = {}         # provider -> the last switch (see ``record_switch``)
_clients: dict = {}          # {"at": monotonic, "view": {...}}


def _moment(value):
    """A datetime in UTC from an ISO time, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- the limit of a product

def observe_turn(provider, kind, resets_at, resets_text, finished_at, status, now=None):
    """The last turn of one agent on ``provider`` ended at ``finished_at`` with ``status`` and ``kind`` (``usage_limit``, ``sign_in``
    or None). A limit marks the product (the newest turn wins); a turn that succeeded after the marked one ends the mark. A turn
    from before the account was last switched or cleared is not looked at. Safe to call again with the same turn."""
    if provider not in PROVIDERS:
        return
    at = _moment(finished_at) or _now(now)
    with _lock:
        floor = _ignore_before.get(provider)
        if floor is not None and at <= floor:
            return
        current = _limits.get(provider)
        if kind == "usage_limit":
            if current is None or at >= current["turn_at"]:
                _limits[provider] = {"turn_at": at, "resets_at": resets_at if isinstance(resets_at, str) else None,
                                     "resets_text": resets_text if isinstance(resets_text, str) else None}
        elif status == "completed" and current is not None and at > current["turn_at"]:
            del _limits[provider]


def limit(provider, now=None) -> dict:
    """``{"reached", "resets_at", "resets_text"}`` of the product. A limit whose reset time has passed is over."""
    now = _now(now)
    with _lock:
        found = _limits.get(provider)
        if found is not None:
            ends = _moment(found["resets_at"])
            if ends is not None and now >= ends:
                del _limits[provider]
                found = None
    if found is None:
        return {"reached": False, "resets_at": None, "resets_text": None}
    return {"reached": True, "resets_at": found["resets_at"], "resets_text": found["resets_text"]}


def clear_limit(provider, now=None):
    """The account changed (or the owner says the limit is over): forget the mark, and every turn that finished up to now."""
    with _lock:
        _limits.pop(provider, None)
        _ignore_before[provider] = _now(now)


# ---------------------------------------------------------------- the last switch

def record_switch(provider, *, previous_account, account, changed, state="restarting", now=None) -> dict:
    """A sign-in on ``provider`` finished: remember the account before and after and when. ``state`` is ``restarting`` while the
    agents are being restarted, then ``done`` (``update_switch``). ``changed`` is false when the same account signed in again."""
    clear_limit(provider, now)
    entry = {"at": _iso(_now(now)), "state": state, "previous_account": previous_account, "account": account,
             "changed": bool(changed), "restarted": 0, "continued": 0, "failed": 0}
    with _lock:
        _switches[provider] = entry
    return dict(entry)


def update_switch(provider, **fields):
    with _lock:
        entry = _switches.get(provider)
        if entry is not None:
            entry.update({key: value for key, value in fields.items() if key in entry})
            return dict(entry)
    return None


def switch(provider):
    """The last switch of ``provider`` as a new dict, or None when none happened since the service started."""
    with _lock:
        entry = _switches.get(provider)
        return dict(entry) if entry is not None else None


# ---------------------------------------------------------------- the accounts as Settings reads them

def remember_clients(view):
    """``owner_settings.agent_clients_view`` was just read: Chat may use it for a short time."""
    if isinstance(view, dict):
        with _lock:
            _clients.clear()
            _clients.update({"at": time.monotonic(), "view": view})


def forget_clients():
    """A sign-in changed: the next read of the clients is a fresh one."""
    with _lock:
        _clients.clear()


def clients_view(loader=None):
    """The newest view of the clients that is younger than ``CLIENTS_TTL``, else a fresh one (``loader``, default
    ``owner_settings.agent_clients_view``). ``{}`` when it cannot be read."""
    with _lock:
        if _clients and time.monotonic() - _clients["at"] < CLIENTS_TTL:
            return _clients["view"]
    try:
        if loader is None:
            from qorqut import owner_settings
            loader = owner_settings.agent_clients_view
        view = loader()
    except Exception:  # noqa: BLE001 - a notice without the account is still a notice
        return {}
    remember_clients(view)
    return view if isinstance(view, dict) else {}


def other(provider):
    return "codex" if provider == "claude" else "claude"


def problem(provider, kind, resets_at, resets_text, loader=None) -> dict:
    """The notice Chat shows in every chat of an agent on ``provider`` whose last turn ended in ``kind`` (``usage_limit`` or
    ``sign_in``): ``{kind, provider, resets_at, resets_text, account: {email, plan} | None, other_provider: {id, ready}}``.
    ``resets_at`` is the product's own when it is limited (the longest known), else the turn's."""
    view = clients_view(loader)
    row = view.get(provider) if isinstance(view.get(provider), dict) else {}
    account = row.get("account") if isinstance(row.get("account"), dict) else None
    shown = {"email": account.get("email"), "plan": account.get("plan")} if account and row.get("signed_in") else None
    rival = view.get(other(provider)) if isinstance(view.get(other(provider)), dict) else {}
    marked = limit(provider)
    if kind == "usage_limit" and marked["reached"]:
        resets_at, resets_text = marked["resets_at"] or resets_at, marked["resets_text"] or resets_text
    return {"kind": kind, "provider": provider, "resets_at": resets_at, "resets_text": resets_text, "account": shown,
            "other_provider": {"id": other(provider), "ready": bool(rival.get("installed") and rival.get("signed_in"))}}


def reset_for_tests():
    with _lock:
        _limits.clear()
        _ignore_before.clear()
        _switches.clear()
        _clients.clear()
