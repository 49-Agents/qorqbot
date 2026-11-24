"""What a failed turn says about the owner's account: a usage limit, or a sign-in that is refused, expired or gone.

``classify_claude`` and ``classify_codex`` are used by the two drivers, which see the clients' own records; ``classify_text`` is
the fallback for a host that started before this existed and reports only the sentence. Each returns None for an error that is
neither (a server problem, a bad request, an interrupted connection) or ``{"kind", "resets_at", "resets_text"}``:

``kind``         ``usage_limit`` or ``sign_in``
``resets_at``    the time the limit ends as an ISO 8601 time in UTC, or None when the client did not say one that can be read
``resets_text``  the client's own words for that time (``Nov 28, 7pm (America/New_York)``, ``Nov 28th, 2026 7:00 PM``,
                 ``in 4 days 3 hours``), or None

What is structured and what is text (checked against Claude Code 2.1.329 and Codex 0.167.0/0.170.0, see the tests):

* Claude Code writes a synthetic assistant message for a failed request with ``error`` (``rate_limit``, ``authentication_failed``,
  ``oauth_org_not_allowed``, ...), ``api_error`` (``usage_limit_reached``, ...) and ``api_error_params.rate_limit_info`` with
  ``status`` and ``resetsAt`` (epoch seconds), and it emits ``rate_limit_event`` records with the same info. Those are used first.
  A ``rate_limit`` without any of them (a plain 429) is not a plan limit and is not labelled. Only when the client sent no code at
  all is the sentence looked at, and only at its start, with the client's known wording.
* Codex ends a failed turn with ``error.codexErrorInfo`` (``usageLimitExceeded``, ``unauthorized``); the reset time comes from the
  newest ``account/rateLimits/updated`` window that is full (``resetsAt``, epoch seconds) and otherwise from the end of its
  sentence (``... try again at Nov 28th, 2026 7:00 PM``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

USAGE_LIMIT, SIGN_IN = "usage_limit", "sign_in"
KINDS = (USAGE_LIMIT, SIGN_IN)
TEXT_LIMIT = 120

# Claude Code's ``error`` codes that mean the account itself is refused, expired or signed out.
CLAUDE_SIGN_IN_ERRORS = frozenset({"authentication_failed", "oauth_org_not_allowed", "account_on_hold", "verification_required"})
CLAUDE_SIGN_IN_API_ERRORS = frozenset({"stored_login_refused", "oauth_refresh_store_refused"})
# ``api_error`` kinds of a ``rate_limit`` that are the plan's own limit (or its credits), not a busy server.
CLAUDE_LIMIT_API_ERRORS = frozenset({"usage_limit_reached", "model_requires_usage_credits", "long_context_credits_required"})

# Codex's ``codexErrorInfo`` names.
CODEX_LIMIT_INFO = frozenset({"usageLimitExceeded"})
CODEX_SIGN_IN_INFO = frozenset({"unauthorized"})

# The client's known sentences, anchored at the start (the fallback only).
_CLAUDE_LIMIT_TEXT = re.compile(r"(?:You(?:'|’)ve|You have)\s+(?:hit|reached)\s+your\s+(?:[\w\- ]{1,30}\s)?limit\b"
                                r"|You(?:'|’)re out of (?:usage )?credits\b|You are out of (?:usage )?credits\b", re.I)
_CLAUDE_SIGN_IN_TEXT = re.compile(r"(?:Not logged in|Login expired|Invalid API key|OAuth token has expired|Please run /login|"
                                  r"Your organization has disabled Claude subscription access)", re.I)
_CODEX_LIMIT_TEXT = re.compile(r"You(?:'|’)ve hit your usage limit|You have hit your usage limit", re.I)
_CODEX_SIGN_IN_TEXT = re.compile(r"(?:Your access token could not be refreshed|Your refresh token|You(?:'|’)re not logged in|"
                                 r"Please log out and sign in again|Please log in again|Not signed in)", re.I)

_CLAUDE_RESETS = re.compile(r"\bresets?\s+(?:at\s+)?(.{1,%d}?)\s*$" % TEXT_LIMIT, re.I)
_CODEX_RESETS = re.compile(r"\btry again\s+((?:at|in)\s+.{1,%d}?)\s*\.?\s*$" % TEXT_LIMIT, re.I)
_CLAUDE_WHEN = re.compile(r"^(?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})(?:,|\s+at)?\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
                          r"(?P<meridiem>am|pm)\s*(?:\((?P<zone>[A-Za-z0-9_/+\-]{1,40})\))?$", re.I)
_CODEX_AT = re.compile(r"^at\s+(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:(?P<year>\d{4}),?\s+)?"
                       r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>am|pm)$", re.I)
_CODEX_IN = re.compile(r"^in\s+(?:(\d+)\s*days?)?\s*(?:(\d+)\s*hours?)?\s*(?:(\d+)\s*(?:minutes?|mins?))?$", re.I)
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch_to_iso(value):
    """An ISO time in UTC for a client's epoch seconds (or milliseconds), None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value / 1000 if value > 1e11 else value
    try:
        return _iso(datetime.fromtimestamp(seconds, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _hour(hour, minute, meridiem):
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    return (hour % 12) + (12 if meridiem.lower() == "pm" else 0), minute


def _month(name):
    return _MONTHS.get(str(name)[:3].lower())


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name) if name else None
    except Exception:  # noqa: BLE001 - an unknown zone name is "no readable time"
        return None


def _local_zone():
    return datetime.now().astimezone().tzinfo


def phrase(text, kind=None, provider=None):
    """The client's own words for the reset time found at the end of its sentence, or None. ``provider`` is ``claude`` or
    ``codex``; with neither both wordings are tried."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    found = None
    if provider in (None, "claude"):
        found = _CLAUDE_RESETS.search(text)
    if found is None and provider in (None, "codex"):
        found = _CODEX_RESETS.search(text)
    return found.group(1).strip(" .")[:TEXT_LIMIT] if found else None


def parse_phrase(words, now=None):
    """An ISO time in UTC for a reset phrase, or None when it cannot be read. Claude's phrase names its zone (``7pm
    (America/New_York)``); Codex's has none and means the time zone of the computer it runs on, which is this one."""
    if not isinstance(words, str) or not words:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        return _parse_phrase(words.strip(), now)
    except (ValueError, OverflowError):
        return None


def _parse_phrase(words, now):
    found = _CODEX_IN.match(words)
    if found and any(found.groups()):
        days, hours, minutes = (int(item or 0) for item in found.groups())
        return _iso(now + timedelta(days=days, hours=hours, minutes=minutes))
    found = _CODEX_AT.match(words)
    if found:
        month, clock = _month(found["month"]), _hour(found["hour"], found["minute"], found["meridiem"])
        if month is None or clock is None:
            return None
        zone = _local_zone()
        year = int(found["year"]) if found["year"] else now.astimezone(zone).year
        return _iso(datetime(year, month, int(found["day"]), clock[0], clock[1], tzinfo=zone))
    found = _CLAUDE_WHEN.match(words)
    if found:
        clock = _hour(found["hour"], found["minute"], found["meridiem"])
        zone = _zone(found["zone"]) if found["zone"] else _local_zone()
        if clock is None or zone is None:
            return None
        local = now.astimezone(zone)
        if found["month"]:
            month = _month(found["month"])
            if month is None:
                return None
            moment = datetime(local.year, month, int(found["day"]), clock[0], clock[1], tzinfo=zone)
            if moment < local - timedelta(days=2):      # "Jan 3" said in December is next year's
                moment = moment.replace(year=local.year + 1)
        else:
            moment = local.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if moment <= local:
                moment += timedelta(days=1)
        return _iso(moment)
    return None


def _reset(provider, text, structured):
    """``(resets_at, resets_text)``: the structured time first, the sentence's own time as the fallback and as the words."""
    words = phrase(text, provider=provider)
    return (structured or (parse_phrase(words) if words else None)), words


def _info(value):
    return value if isinstance(value, dict) else {}


def classify_claude(error=None, api_error=None, status=None, rate_limit_info=None, text=None):
    """The label of a failed Claude Code turn from what the CLI reported: ``error`` and ``api_error`` of its synthetic assistant
    message, the HTTP ``status`` (None when the request never got an answer), ``rate_limit_info`` (the message's
    ``api_error_params.rate_limit_info`` or the last ``rate_limit_event``) and the sentence ``text`` (used only as the reset words
    and, when the CLI sent no code at all, to recognise its known sentence)."""
    info = _info(rate_limit_info)
    structured = epoch_to_iso(info.get("resetsAt"))
    if isinstance(error, str) and error:
        if error in CLAUDE_SIGN_IN_ERRORS or api_error in CLAUDE_SIGN_IN_API_ERRORS:
            return {"kind": SIGN_IN, "resets_at": None, "resets_text": None}
        if error == "rate_limit" and (api_error in CLAUDE_LIMIT_API_ERRORS or info.get("status") == "rejected"):
            resets_at, words = _reset("claude", text, structured)
            return {"kind": USAGE_LIMIT, "resets_at": resets_at, "resets_text": words}
        return None
    if isinstance(api_error, str) and api_error in CLAUDE_SIGN_IN_API_ERRORS:
        return {"kind": SIGN_IN, "resets_at": None, "resets_text": None}
    if isinstance(api_error, str) and api_error in CLAUDE_LIMIT_API_ERRORS:
        resets_at, words = _reset("claude", text, structured)
        return {"kind": USAGE_LIMIT, "resets_at": resets_at, "resets_text": words}
    return classify_text("claude", text) if not api_error else None


def classify_codex(info=None, text=None, rate_limits=None):
    """The label of a failed Codex turn from its ``codexErrorInfo`` name and the newest rate-limit snapshot of the thread (the
    ``rateLimits`` of ``account/rateLimits/updated``), the sentence ``text`` giving the reset words and, when Codex named no
    error kind, its known wording."""
    if isinstance(info, str) and info:
        if info in CODEX_SIGN_IN_INFO:
            return {"kind": SIGN_IN, "resets_at": None, "resets_text": None}
        if info in CODEX_LIMIT_INFO:
            resets_at, words = _reset("codex", text, _codex_window_reset(rate_limits))
            return {"kind": USAGE_LIMIT, "resets_at": resets_at, "resets_text": words}
        return None
    return classify_text("codex", text)


def _codex_window_reset(snapshot):
    """The latest ``resetsAt`` of a window of the snapshot that is full (``usedPercent`` 100 or more), as an ISO time."""
    snapshot = _info(snapshot)
    times = []
    for name in ("primary", "secondary"):
        window = _info(snapshot.get(name))
        used, resets = window.get("usedPercent"), window.get("resetsAt")
        if isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 100 and epoch_to_iso(resets):
            times.append(resets)
    return epoch_to_iso(max(times)) if times else None


def classify_text(provider, text):
    """The fallback: the client's known sentence at the start of the error text (nothing else in the text counts)."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    limit, sign_in = ((_CLAUDE_LIMIT_TEXT, _CLAUDE_SIGN_IN_TEXT) if provider == "claude" else (_CODEX_LIMIT_TEXT, _CODEX_SIGN_IN_TEXT))
    if limit.match(text):
        resets_at, words = _reset(provider, text, None)
        return {"kind": USAGE_LIMIT, "resets_at": resets_at, "resets_text": words}
    if sign_in.match(text):
        return {"kind": SIGN_IN, "resets_at": None, "resets_text": None}
    return None


def label_of(last_turn, provider):
    """The label of a host's ``last_turn`` record: its own ``error_kind`` when the driver set one, otherwise (a host that started
    before this existed) the text fallback. None when the turn did not fail or the failure is neither kind."""
    if not isinstance(last_turn, dict) or last_turn.get("status") != "error":
        return None
    kind = last_turn.get("error_kind")
    if kind in KINDS:
        resets_at, words = last_turn.get("resets_at"), last_turn.get("resets_text")
        return {"kind": kind, "resets_at": resets_at if isinstance(resets_at, str) else None,
                "resets_text": words if isinstance(words, str) else None}
    if "error_kind" in last_turn:
        return None                     # the driver looked and found neither
    reason = last_turn.get("reason")
    if provider == "codex" and isinstance(reason, str) and reason:
        return classify_codex(reason, last_turn.get("error"))
    return classify_text(provider, last_turn.get("error"))
