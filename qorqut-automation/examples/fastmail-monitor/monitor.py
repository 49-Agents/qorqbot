"""Quiet Fastmail monitor (source-only example; never installed, enabled or run by this repository).

What the installed engine returns (read from its source, not run): ``fastmail_messages_search`` takes no account id and returns **ids
only** - ``{"ids", "position", "total", "query_state"}`` - newest ``receivedAt`` first, paged by ``limit`` (at most 100) and ``position``;
``after`` is inclusive. ``fastmail_message_get`` returns ``{"message": {...}}`` with ``subject``, ``from.emailAddress`` and
``receivedDateTime``. The monitor therefore never relies on message fields from a search page.

* The first run records a *baseline*: the ids of everything already in the overlap window, kept in ``seen`` with a stable
  ``baseline_at`` marker, and publishes nothing. Because the baseline is a set of ids (not a time), those messages stay quiet in every
  later overlap scan, while the very first message after an *empty* mailbox is still new.
* Later runs read the whole window since ``cursor`` minus an overlap (late delivery, shared timestamps) as one consistent snapshot. A
  moving ``position`` scan can skip a message when older mail disappears between pages (or repeat one when new mail arrives), so every
  page must report the same ``query_state`` and ``total``; any change restarts the window read (bounded attempts, then the run ends
  without moving the cursor and the next run reads again).
* A message is new when its id is not in ``seen``; each new message is published once with the stable event key ``mail-<id>`` (publishing
  is idempotent per key across runs, so even a lost ``seen`` never duplicates an event).
* Only after every new message of the window was published does ``cursor`` advance (to the time the window read began) and ``seen`` become
  exactly the ids of the window just read, which also retires the baseline once it leaves the window.
* Work is bounded per invocation; a long one continues with ``run.continue_run`` and replays its recorded steps.
"""

from datetime import datetime, timedelta, timezone

import qorqut_automation_sdk as sdk

PAGE = 50
MAX_SCAN = 800          # messages examined per window; more is refused rather than guessed (also keeps the saved ``seen`` set well under 64 KiB)
MAX_ATTEMPTS = 3        # whole-window re-reads when the mailbox changed under a moving offset scan
DETAILS = 100           # new messages whose subject and sender are fetched; the rest are published by id
OVERLAP = timedelta(hours=1)
MARGIN_SECONDS = 10


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _folder(run):
    account = run.call("fastmail_capabilities_get", {}, key="account")
    wanted = run.input.get("account_id")
    if wanted and wanted != account.get("account_id"):
        raise ValueError("the installed Fastmail account is not the one this automation was created for")
    name = str(run.input.get("mailbox", "Inbox")).lower()
    folders = run.call("fastmail_folders_list", {}, key="folders").get("folders", [])
    found = [f for f in folders if str(f.get("role", "")).lower() == name or str(f.get("name", "")).lower() == name]
    if len(found) != 1:
        raise ValueError("expected exactly one Fastmail folder named %r, found %d" % (name, len(found)))
    return account.get("account_id"), found[0]["id"]


def _scan(run, folder_id, since, scan_at):
    """Every id received since ``since``, newest first, from one unchanged query state; ``None`` if the mailbox kept changing."""
    for attempt in range(MAX_ATTEMPTS):
        ids, first, position = [], None, 0
        while True:
            if run.remaining() < MARGIN_SECONDS:
                run.continue_run({"scan_at": scan_at})        # the recorded pages replay next time; nothing is read twice
            page = run.call("fastmail_messages_search",
                            {"in_mailbox": folder_id, "after": since, "limit": PAGE, "position": position},
                            key="scan-%s-%d-%d" % (since, attempt, position // PAGE))
            total, found = page.get("total"), page.get("ids")
            if not isinstance(total, int) or not isinstance(found, list):
                raise ValueError("the engine returned a search page without ids and a total")
            marker = (page.get("query_state"), total)
            if first is None:
                if total > MAX_SCAN:
                    raise ValueError("%d messages since %s; shorten the interval or the overlap instead of guessing" % (total, since))
                first = marker
            elif marker != first:
                break                       # new mail arrived or older mail vanished between pages: offsets moved, read it all again
            ids.extend(found)
            position += len(found)
            if not found or position >= total:
                return list(dict.fromkeys(ids))
    return None


def work(run):
    run.phase("scan")
    progress = run.progress or {}
    scan_at = progress.get("scan_at") or _iso(datetime.now(timezone.utc))      # fixed for the whole run, across its invocations
    account_id, folder_id = _folder(run)
    saved = run.saved
    since = _iso(_stamp(saved["cursor"] if saved.get("initialized") else scan_at) - OVERLAP)
    ids = _scan(run, folder_id, since, scan_at)
    if ids is None:
        return {"unstable": True}           # the cursor did not move: the next run reads the same window again
    if not saved.get("initialized"):
        # Quiet baseline: these ids already existed. A message that arrives later is not among them, even in an empty mailbox.
        run.save({"initialized": True, "baseline_at": scan_at, "cursor": scan_at, "seen": {i: scan_at for i in ids}})
        return {"baseline": True, "known": len(ids)}
    seen = dict(saved.get("seen") or {})
    fresh = [i for i in reversed(ids) if i not in seen]         # oldest first
    for index, message_id in enumerate(fresh):
        if run.remaining() < MARGIN_SECONDS:
            run.continue_run({"scan_at": scan_at})
        detail = {}
        if index < DETAILS:
            message = run.call("fastmail_message_get", {"message_id": message_id}, key="get-%s" % message_id).get("message") or {}
            sender = (message.get("from") or {}).get("emailAddress") or {}
            detail = {"subject": message.get("subject"), "from": sender.get("address"), "received_at": message.get("receivedDateTime")}
        run.publish("mail-%s" % message_id, "New mail: %s" % (detail.get("subject") or "(no subject)"),
                    data={"account_id": account_id, "message_id": message_id, **detail})
    # Every new message of the window is published: only now does the cursor advance (never backwards) and ``seen`` shrink to the window.
    cursor = max(saved["cursor"], scan_at, key=_stamp)
    run.save({"cursor": cursor, "seen": {i: seen.get(i, scan_at) for i in ids}})
    return {"published": len(fresh), "cursor": cursor}


sdk.main(work)
