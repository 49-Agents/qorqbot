# Release candidate 1.3.0-preview.3

This is a candidate, **not a published release**. Nothing here uploads, tags,
deploys or replaces a website download. Preview 1 and 2 downloads are unchanged.

## Contents and source

The candidate is built from one exact commit of the publication repository, which
`SOURCE-MANIFEST.json` records, with the runbook's export, projection, audit,
build and check steps. Targets: Ubuntu 24.04 x86-64 TAR.GZ, Windows ZIP wrapping
that exact Linux archive for WSL2 Ubuntu 24.04, and Apple silicon macOS 14+
TAR.GZ and DMG. Changes since the preview 2 tag are the merged pull requests
through #26, including the Focus/Signoff and notification refinements, Todo owner
toggle, removal of the per-Signoff thread button (#25) and the terminal Enter
retry when the same message stays pending (#26).

## Limits

- Native Windows/WSL2 host acceptance has not been performed; the ZIP is a
  handoff wrapper, not an installer.
- GitHub Actions checks the Mac archive and DMG build and exercises the packaged
  launcher on a `macos-15` Apple-silicon runner (the `macos-14` runner was
  rejected as unsupported when dispatched on 2026-11-07). The package target stays `macos14-arm64` (macOS 14 wheels),
  but automated acceptance ran on macOS 15; native macOS 14 acceptance is
  unverified. It does not prove the downloaded app's Gatekeeper/Open Anyway flow,
  a separate macOS user account or interactive agent sign-in. The DMG is ad-hoc
  signed, not notarized.
- Updates are manual (`./qorqbot update ARCHIVE --sha256 HASH`) and refuse changed
  database schemas or managed instruction contents, running terminals and
  installations with separately installed connectors. The embedded database
  schema target differs from preview 2's, so the updater is expected to refuse a
  preview 2 to 3 update and no migration is provided; this comparison was of
  package metadata, not a run against a preview 2 installation. Preview 1 was
  not compared. Do not promise that an existing installation can update.
- Optional connector bundles are separate downloads and are not part of this
  candidate unless rebuilt and checked against its final archive.
