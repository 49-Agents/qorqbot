# Release 1.3.2

Published on November 14, 2026 as a
[GitHub release](https://github.com/49-Agents/qorqbot/releases/tag/v1.3.2).
The limits below describe that download; publication does not qualify additional
platforms.

## Contents and source

The release is built from one exact commit of this repository, which
`SOURCE-MANIFEST.json` records, with the runbook's export, projection, audit,
build and check steps. Targets: Ubuntu 24.04 x86-64 TAR.GZ, Windows ZIP wrapping
that exact Linux archive for WSL2 Ubuntu 24.04, and Apple silicon macOS 14+
TAR.GZ and DMG.

Qorqbot asks the owner to sign in once per installation with an emailed code; see
[the account notes](../ACCOUNT.md). The application is licensed under the PolyForm
Noncommercial License 1.0.0, with a per-person commercial license that is free
until 2027-01-01; see [the license scope](../../LICENSE-SCOPE.md).

## Changes since 1.3.1

- Sign-in: the emailed code is six digits. Settings shows the account's license
  status from the sign-in server: a paid license with its plan and paid-through
  date, the free commercial period for work use, or that the status is not known
  yet. The status is shown only; nothing is blocked.
- Set-up starts on the owner's first message, with one optional form that asks for
  a todo list and quick context. A first walkthrough ends by typing and sending the
  first hello.
- Focus holds the owner's notes and pinned threads only, with notes text 7%
  larger; threads render like Chat, and items opened on Focus cover the notes. An
  agent with Todo work and no focus is asked to set one.
- Chat: threads and side chats open in the left chat, side chats take files and
  their messages can reference Signoffs and Tasks, the focus line sits at the top
  of the chat input and chat text is 10% larger.
- Owner messages, a day review, read-and-acknowledge Signoffs and a daily report.
  Record IDs and revisions are kept out of what the owner reads.
- Calendar: one toolbar, a colour per calendar and a line for the current time;
  calendar chips retire two hours after their event ends.
- Files show projects and notes, with system files behind a switch. The Journal
  page is left-aligned in the agent pane.
- Connections lists Qorqbot's extensions instead of Claude connector cards, and the
  treg toolkit is bundled.

## Limits

- Native Windows/WSL2 host acceptance has not been performed; the ZIP is a
  handoff wrapper, not an installer.
- GitHub Actions checks the Mac archive and DMG build and exercises the packaged
  launcher on a `macos-15` Apple-silicon runner. The package target stays
  `macos14-arm64` (macOS 14 wheels), but automated acceptance ran on macOS 15;
  native macOS 14 acceptance is unverified. It does not prove the downloaded
  app's Gatekeeper/Open Anyway flow, a separate macOS user account or
  interactive agent sign-in. The DMG is ad-hoc signed, not notarized.
- Updates are manual (`./qorqbot update ARCHIVE --sha256 HASH`) and refuse changed
  database schemas or managed instruction contents, running terminals and
  installations with separately installed connectors. 1.3.2 changes managed agent
  instructions and the database schema, so the updater refuses a 1.3.1 or 1.3.0
  installation and leaves it on its version with its data. Install 1.3.2 as a
  fresh installation; no migration from another download is provided.
- Sign-in from 1.3.1 and 1.3.0 expects the earlier code format and no longer
  completes against the sign-in server; installations already signed in are not
  affected.
- Optional connector bundles are separate downloads and are not part of this
  release unless rebuilt and checked against its final archive.
