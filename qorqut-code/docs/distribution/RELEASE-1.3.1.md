# Release 1.3.1

Published on November 13, 2026 as a
[GitHub release](https://github.com/49-Agents/qorqbot/releases/tag/v1.3.1).
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

## Changes since 1.3.0

- First run: a new installation creates the Context store, so Ordubashy's set-up
  conversation can save the owner's answers, and agents in folder and desktop
  installations receive their session binding. The walkthrough holds the agent
  menu open for the steps that point into it, places its card by text size,
  pauses on Esc and can be replayed. The sign-in screen keeps the page behind it
  inert and shows a resent code as a status message.
- Calendar events join the attention strip beside reminder bells and stay until
  the owner dismisses them; the Focus calendar row is removed.
- Long Signoffs keep their decision controls in view while the body scrolls.
- Consecutive inbox nudges and agent messages share one compact activity row
  with agent icons, and consecutive agent references in Chat sit side by side.
- URLs the owner writes stay plain links without generated cards.
- Ordubashy has a Files tab while Testing mode is on.

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
  database schemas, running terminals and installations with separately
  installed connectors. 1.3.1 keeps the database schema of 1.3.0, so a stopped
  1.3.0 installation can be updated in place and keeps its data; the launcher
  then creates the Context store once if the installation has none. No migration
  from any other download is provided.
- Optional connector bundles are separate downloads and are not part of this
  release unless rebuilt and checked against its final archive.
