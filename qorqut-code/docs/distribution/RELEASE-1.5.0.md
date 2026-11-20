# Release 1.5.0

> **Historical record.** Agents run as managed sessions by default. The terminal mechanism described below (tmux, the terminal view, typed client commands) is the legacy Terminals mode, an option for the whole installation; see [managed agent sessions](../MANAGED-RUNTIME.md).

Published on November 18, 2026 as a
[GitHub release](https://github.com/49-Agents/qorqbot/releases/tag/v1.5.0).
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

## Changes since 1.4.0

- Timelines: every Task, Project and the Global level has a timeline of goals,
  decisions, facts and progress, plus a list of open questions for the owner.
  Ordubashy files them, quoting the owner's words, and each filing waits for the
  owner's review before it applies. Agents receive what holds now for a Task and
  every level above it once per conversation. The owner sees a panel above Task
  threads, a Global block above the main chat and an Open questions page.
- Agents' working memory, the old plan files and agents' private notes are
  removed as features; stored data stays untouched.
- Context: filing can run on the Claude Code CLI instead of the Codex app-server
  (set the runner's `executable` to `claude` and an exact Claude model id). Search
  ranking, the start-up Context text and the owner's personal facts reach agents
  more reliably, and agents ask the owner for a personal fact once.
- Reports belong to a Task, and pinned threads and report threads are tidier.
- Chat: Project references are clickable, reference chips show in threads, and
  choice cards and approval option labels fit their space. Replies are shorter.
- Notifications have a close button and close themselves after 10 seconds.
- Agents can switch into iframes with the browser frame command. The viewbox tool
  is retired.
- Tasks that carry fields from older data can be updated and closed again.
- Instruction-history checks stay fast, and a retired instruction text no longer
  breaks the verification of older updates. Recovery after a conversation
  compaction is more reliable.

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
  installations with separately installed connectors. 1.5.0 changes managed agent
  instructions and the database schema, so the updater refuses a 1.4.0 or
  earlier installation and leaves it on its version with its data. Install 1.5.0 as a fresh installation; no
  migration from another download is provided.
- Optional connector bundles are separate downloads and are not part of this
  release unless rebuilt and checked against its final archive.
