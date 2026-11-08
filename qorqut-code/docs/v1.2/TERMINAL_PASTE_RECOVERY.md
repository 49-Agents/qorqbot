# Terminal paste verification and recovery

A strict native input can reach the terminal composer before the CLI redraws it.
The transport observes the paste after 300 ms, then permits up to nine further
read-only observations 200 ms apart. Every observation verifies the same client,
pane and terminal mode. A changed identity or an interrupting submit key stops
the attempt. An exact paste authorizes Enter. After 400 ms, if the same paste is
still in the ordinary composer of the same registered session, the transport
presses Enter once more. It never repastes or clears text during this retry.
A cleared or different draft, changed session, modal, interrupting mode or
unreadable screen prevents the second Enter. If the paste remains after the
second attempt, delivery stays `uncertain` with `submit_unconfirmed`. Receipts
record `submit_attempts` (1 or 2), without message or screen text. This bounded
retry happens inside the original delivery attempt; a saved uncertain input is
never replayed. Free-form Chat's existing optional-echo behavior is unchanged.

If verification still fails, the input stays `uncertain` with `paste_unverified`.
Its receipt includes `paste_check` and `paste_checks`, alongside the existing
terminal/composer fingerprints. No screen text or message body is added to
diagnostic receipts. Subsequent notices may defer with `composer_not_empty`.

## Automatic receipt confirmation for Todo reminders

The bounded native transcript scan can confirm a Todo reminder that arrived after
an uncertain paste attempt. It matches the complete marked notice to its saved
input in the same agent, provider, conversation and transcript, after admission
and before session end. It sends no text and presses no keys. The original
uncertain transport history is retained with `transcript_confirmed` evidence;
processing or completion is never inferred. Once confirmed, the reminder consumes
that receipt once and the existing clock schedules its next occurrence. A late
receipt cannot alter a newer focus or shorten a later owner-message deadline.

An unmatched paste remains uncertain. The manual procedure below is still needed
when the original notice is held in the composer; no automatic retry is added.

## Recovering an existing system notice

This is an owner-authorized recovery action, not an automatic retry of uncertain
input. Inspect the registered live terminal and the original input record first.
For an `inbox_nudge`, match its full body and system marker, agent, native
conversation, process start identity and pane. Verify that the marked notice
has not already appeared in the native transcript, and that the ordinary
composer contains exactly that notice with no extra text. Repeat the read to
check stability. A modal, different draft, changed identity or uncertain match
requires inspection; do not clear it or submit a replacement.

Once that exact pending paste is verified and recovery is authorized, submit
the existing text once. Preserve the original uncertain receipt as history;
do not edit SQLite to claim delivery. Confirm the marker appears in the same
native transcript and that the agent actually reads/acknowledges the intended
inbox events. Do not restart the native session, replay business actions or
change task holds as part of terminal recovery.

The October 17, 2026 Atlas repair used this procedure. The held text was the
system's earlier unsent notice. Recovery preserved the native conversation,
and Atlas acknowledged the browser-check request and successfully used its
restored own-browser binding. Private execution evidence remains under the
instance's `state/atlas-browser-nudge-repair/`, outside Git.

Focused verification: `app/tests/test_terminal_enter_retry.py` covers missed
Enter, successful submission, bounded failure, changed sessions and composers,
and busy/interrupting modes. The `app/tests/test_terminal_composer_*.py` fixtures
cover the supported client layouts, paste placeholders and command hints.
