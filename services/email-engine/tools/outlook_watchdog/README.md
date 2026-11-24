# Outlook mailbox watchdog

This is the existing standalone watchdog (baseline `ac190295418a362a0e32f6ff31685e63f0b2c91b`),
now maintained beside the email engine. It inspects the actual Outlook mailbox,
not just Chrome's process or `/health/live`.
The named sender tab takes precedence over unrelated broken tabs; page checks
run concurrently under bounded deadlines. A failing named sender remains unhealthy.

## Detection and recovery

- The timer runs 30 seconds after each completed check. The read-only probe has
  a 17-second outer deadline and never takes the sender lock. Two failed samples
  within two minutes qualify for recovery (normally about a minute; at most
  roughly 95 seconds to the second result with normal timer scheduling).
- Only a restart takes the engine's existing `.outlook.lock`. The watchdog waits
  at most eight seconds each tick, then records `recovery_wait`. After two minutes
  of waiting it reports `needs_attention` and raises an urgent notification.
  Detection continues throughout. It never interrupts an in-flight send.
- After obtaining the lock, probe again before restarting. Restart only the
  dedicated Outlook browser and verify while retaining the lock. Preserve the
  30-minute cooldown and failed-recovery latch. Login/configuration errors need
  attention, not repeated restarts. A healthy probe clears the latch.
- Failed recovery, login/configuration problems and cooldown-blocked recovery
  raise one incident via the existing Qorqut local Telegram notification gateway.
  Recovery updates that incident. An unsuccessful gateway call is retried next
  tick with the same event key. Gateway acceptance is recorded as `queued`, not
  proof of Telegram delivery. No email is used to alert about email failure.
- Intentionally inactive engine/browser services are left inactive. No ledger
  writes, approval extensions, test emails, or retries of uncertain sends occur.

## Health and Paper/integration reads

`state.json` remains private (0600). The same directory contains a credential-free
`health.json` (0644), so the directory must be traversable (0755). The public
snapshot contains only account reference, status, timestamp, reason, recovery
state and notification status. It contains no mailbox address, tokens or mail.

Set `EMAIL_OUTLOOK_HEALTH_FILE=/var/lib/qorqut-outlook-watchdog/health.json` in the
email-engine service environment when deploying the corresponding engine change.
The engine treats missing, malformed, wrong-account, future-dated or older-than-
two-minute snapshots as `unknown`; these never produce `send_available=true`.
A known provider failure also takes precedence over a healthy UI sample until
a successful bridge operation clears it.
Unconfigured installations retain their existing behavior.

The existing `/mailboxes` shared-sender metadata adds `health` and uses it for
`send_available`. Scoped `/integrations/routes` responses add `sender_health`
and `send_available`. Consumers should report “Outlook unavailable; emails queued”
for unavailable health, and “Outlook health unknown” for stale/missing health.
A durable route's `ready`, revision, generation and blocker are unchanged: route
provisioning is separate from live sender health. This avoids invalidating
campaign pins or revoking approvals because of a transient tab crash. These
fields expose the information to Paper/agents; they do not alter Paper's stored
campaign lifecycle or claim that handoff means delivery. `/health/live` remains
process liveness only.

## Installation

Prepare and review a committed release first; do not point root systemd services
at a writable worktree. Install the scripts into a root-owned immutable directory,
render `@INSTALL_DIR@` in the service, and install both units. Retain the existing
state and cooldown. This service is independent of the host app's native sessions.

Configure a dedicated enabled producer in the Telegram configuration actually read by
the live app (including its mount namespace). A stale copy of that configuration is not sufficient.
The root watchdog requires an explicit `peer_uid: 0` binding.
Keep its credential in a root-owned 0600 file. The private file
`/etc/qorqut-email/outlook-watchdog-notify.json` contains:

```json
{
  "socket_path": "/run/qorqut-telegram/notify.sock",
  "secret_file": "/etc/qorqut-email/outlook-watchdog-notify.secret"
}
```

These are example paths, not a supplied credential or an installed producer.
If the existing gateway socket lives below `/home`, retain `ProtectHome=yes` and
bind only that directory to a destination outside `/home`, for example
`BindReadOnlyPaths=/home/owner/state/telegram:/run/outlook-watchdog-notify`.
Set `socket_path` to `/run/outlook-watchdog-notify/notify.sock`. Binding back
under the inaccessible home parent does not make the socket reachable. Verify
`--check-notifications` with the service sandbox properties, not just a root shell.
A missing/unreachable notifier is recorded as `notification_status=failed`, never
as a delivered alert. Use `--check-notifications` to verify authenticated gateway
access without posting an alert. Verify gateway authorization and actual owner delivery
before treating alerting as operational; do not manufacture an outage on the
live sender to test it.

After installing, reload systemd and restart the watchdog timer (not Chrome or
Paper). Run the hardened watchdog once, confirm the fresh health snapshot is
readable as the engine user, check sender metadata, and verify the gateway path.
An attention state or failed notification exits nonzero for systemd visibility.
A healthy sender with `notification: failed` means alert delivery needs repair;
it does not mean Outlook itself crashed.
`--check-only` probes without acquiring the sender lock, saving state, restarting
or notifying. Disable using `systemctl disable --now qorqut-outlook-watchdog.timer`.

## Verification

Run from the repository root in an environment with the email-engine dependencies:

```sh
python3 services/email-engine/tools/outlook_watchdog/test_watchdog.py
node --test services/email-engine/tools/outlook_watchdog/test_probe.mjs
PYTHONPATH=services/email-engine python3 -m unittest discover -s services/email-engine/tests
```

Checks use synthetic tabs, real temporary flock files, a temporary Unix socket,
and temporary health snapshots. They cover an unresponsive/crashed renderer,
login-required state, busy-sender detection and restart fencing, failed recovery,
notification retry/deduplication/recovery, stale health, and unchanged route pins.
They do not send production messages or restart live services.
