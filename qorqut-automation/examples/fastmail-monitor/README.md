# Fastmail monitor (source-only example)

Not installed, enabled, scheduled or run by this repository. It shows the intended shape of a quiet mail monitor on the automation SDK.

What the installed engine does (read from the engine's source, not executed): `fastmail_messages_search` accepts no account id, only a
folder (`in_mailbox`), `after`/`before` (UTC date-times; `after` is inclusive) and bounded `limit` (at most 100) / `position`. It returns
**ids only** - `{"ids", "position", "total", "query_state"}` - sorted newest `receivedAt` first. `fastmail_message_get` returns
`{"message": {...}}` with `subject`, `from.emailAddress.address` and `receivedDateTime`. The example reads those names and nothing a
search page does not carry.

* the account and folder are resolved with the existing read tools (`fastmail_capabilities_get`, `fastmail_folders_list`);
* the first run records a **baseline** and publishes nothing: the ids already in the overlap window go into the saved `seen` set under
  a stable `baseline_at`. The baseline is a set of ids, not a timestamp, so those messages stay quiet in every later overlap scan, and
  the first message that arrives after an *empty* mailbox is still new;
* later runs read the window since the cursor **minus a one-hour overlap** (late delivery, shared timestamps) as one consistent
  snapshot. A moving `position` scan over a newest-first list can *skip* a message when older mail disappears between pages (and repeat
  one when new mail arrives), so every page must report the same `query_state` and `total`; any change restarts the whole window read
  (three attempts, then the run ends without moving the cursor and the next run reads the window again);
* a message is new when its id is not in `seen`. Each new message is published once under the stable event key `mail-<id>`; publishing is
  idempotent per key across runs, so even a lost `seen` never duplicates an event;
* the cursor advances **only after every new message of the window was published**, to the time the window read began (never backwards);
  `seen` then becomes exactly the ids of that window, which also retires the baseline once it leaves the window;
* a run that exceeds the 60-second invocation ceiling continues explicitly (`run.continue_run`) and replays its recorded steps;
  a window of more than 800 messages is refused rather than guessed;
* a run that finds no new mail publishes nothing, so an unchanged mailbox never wakes anyone.

To use it, the managing agent creates an automation with `automations_create` (`script_source` from `monitor.py`, a declared `scan`
phase, `arguments` such as `{"mailbox": "Inbox"}`), and schedules it with `automations_schedule_set`. Ordubashy needs the Fastmail
read capability; the script only reads. Mail that arrives while the baseline window is being read counts as baseline. Fetching the
subject and sender of a new message reads its whole message once (bounded to the first 100 new messages of a window).
