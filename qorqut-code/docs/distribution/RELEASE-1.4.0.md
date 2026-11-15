# Release 1.4.0

Published on November 15, 2026 as a
[GitHub release](https://github.com/49-Agents/qorqbot/releases/tag/v1.4.0).
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

## Changes since 1.3.2

- Mac: the download is self-contained. The archive carries its own Python 3.12
  with the libraries installed, plus tmux, git and the agent-browser tool, so
  nothing has to be installed first: no Homebrew, Python, tmux or Apple developer
  tools. First launch unpacks, checks the package and creates the records; it
  builds and downloads nothing. See [the Mac guide](MACOS.md).
- Mac app: the programs checklist is replaced by a short "Setting up Qorqbot"
  screen, and the log appears only when something fails. Opened from the disk
  image, Downloads or the Desktop, the app offers to move itself to Applications.
  The disk image opens as a window with the app, an arrow and the Applications
  folder.
- Delegated Tasks: Ordubashy can hand a worker a Task with a step-by-step plan,
  written or from a saved template, and a worker's Todo is an ordered queue. A
  Task with a plan shows one line on its thread, with the steps behind "Show
  steps".
- Website sign-in: Ordubashy signs in to websites itself, in its own or a
  worker's browser, and asks the owner only for the password or code it lacks.
- Worker browsers start when the service sees its instance through a private
  mount, and with deep state folders.
- Timelines show the owner's own messages between the recorded actions, and a
  daily report shows its timeline under Ordubashy's text.
- Focus: retry buttons no longer stretch the attention row.

## Limits

- Native Windows/WSL2 host acceptance has not been performed; the ZIP is a
  handoff wrapper, not an installer.
- GitHub Actions checks the Mac archive and DMG build on a `macos-15`
  Apple-silicon runner, then opens the built disk image on clean `macos-15` and
  `macos-26` runners with Homebrew, Python, tmux, Node.js and Apple's developer
  tools taken away; the app must move itself to Applications, set itself up and
  show the sign-in screen. The package target stays `macos14-arm64`; native
  macOS 14 acceptance is unverified. The checks do not prove the downloaded app's
  Gatekeeper/Open Anyway flow, a separate macOS user account or interactive agent
  sign-in. The DMG is ad-hoc signed, not notarized. There is no Intel Mac
  package.
- Updates are manual (`./qorqbot update ARCHIVE --sha256 HASH`) and refuse changed
  database schemas or managed instruction contents, running terminals and
  installations with separately installed connectors. 1.4.0 changes managed agent
  instructions, so the updater refuses a 1.3.2 or earlier installation and leaves
  it on its version with its data. Install 1.4.0 as a fresh installation; no
  migration from another download is provided.
- Optional connector bundles are separate downloads and are not part of this
  release unless rebuilt and checked against its final archive.
