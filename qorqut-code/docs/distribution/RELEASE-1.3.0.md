# Release 1.3.0

Published on November 13, 2026 as a
[GitHub release](https://github.com/49-Agents/qorqbot/releases/tag/v1.3.0).
The limits below describe that download; publication does not qualify additional
platforms or establish compatibility with an existing installation.

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
  installations with separately installed connectors. No migration from another
  download is provided; install 1.3.0 as a fresh installation.
- Optional connector bundles are separate downloads and are not part of this
  release unless rebuilt and checked against its final archive.
