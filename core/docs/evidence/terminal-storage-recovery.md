# D60 live terminal recovery — 2026-10-10

Core `e33d22b0630e83dcd0389f98344f051e9dddc7ec` is merged, pushed and
installed in the local qorqutbot test application. The unchanged UI is
`fd96ee4140bc0ddaa69a15cd6355fd3e17b3a80e`. The deployed wheel SHA-256 is
`01be25dfdee780a9a4c29fe1bc214664fe0976a633e18520efc51a59edf7f059`;
all 93 packaged files matched the installation. The owner requested no review
rounds. Focused implementation and installed-fixture checks are recorded in
[terminal-storage-fix.md](terminal-storage-fix.md).

## Recovery performed

Before retirement, all **40,945,718 committed terminal bytes** were archived in
13,239 host ranges, with zero retained local payload and zero transfer intents.
The 1,173,847 original local receipt rows were preserved. No delivery attempt was
prepared, dispatching or uncertain; all five were submitted. No native owner was
open. The original recorder, Bash and Codex processes were absent. The exact
saved tmux server/socket fingerprints matched, and its only pane was dead with
exit code 74.

With core stopped, Manager backed up the host/native databases and managed hook
configuration, installed the wheel, stopped only that corroborated dead-pane
tmux server, checkpointed the original terminal database and retained it together
with its input locks and tmux metadata. Its saved failed health and incomplete
recorder finalization remain unchanged. Existing initial creation established a
fresh recorded Bash shell with a new terminal/source/epoch. No native CLI was
started or resumed and no owner input or message replay was sent.

The old core service was transient and became unloaded when its last surviving
process, the old tmux server, exited. The first restart therefore returned unit
not found. Manager restored a persistent user unit with the same executable,
installation, UI and `KillMode=process`, then started it. Boot autostart was not
enabled. This operational definition is outside the repository.

HOME, workspace, native SQLite and browser profile remained at the same paths
and inodes; browser desktop metadata remained byte-identical. Host messages,
conversation entries, session owners and delivery attempts retained their counts
(7 / 25 / 1 / 5), and the complete original archive epoch remained unchanged.
Native setup refreshed the owned hook files to the new terminal binding.

## Verification and limits

At 18:17:50 UTC, `terminal_get` reported available/verified and healthy recording;
the new Bash prompt's 18 captured bytes were already archived. Native setup was
ready with no connected model, and the browser remained available. The external
HTTPS page matched the installed UI and core readiness returned HTTP 200.
Default Codex browser control returned no browsers, so this is backend and
serving verification, not a claimed hands-on browser UX test.

The preserved native capture still reports its pre-existing parser gap, with zero
pending archive/parse bytes. This repair does not claim complete native semantic
coverage or recover bytes that never committed. The original SQLite exception
remains unknown; the deployed fix addresses the measured query-scaling defect.
The owner can refresh Terminal and manually resume the saved Codex session.

Private operational evidence, commands, before/after state and recovery location
are recorded under the build-state directory's `terminal-storage-deployment/`.
The retained runtime and databases are in the local installation's
`backups/terminal-recovery-20261010T181553Z/`. No operational data or transcript
contents were committed to Git.
