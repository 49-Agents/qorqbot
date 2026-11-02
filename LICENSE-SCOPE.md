# License scope

Copyright (c) 49 Agents LLC. Qorqbot's first-party code, documentation, agent
instructions and connector adapters use the Business Source License 1.1 in
`LICENSE`, unless a file or component explicitly carries another license.

Personal use and internal business use are permitted without revenue or size
thresholds. Competing production offerings require a separate license. Copying,
modification and redistribution remain subject to the standard BSL terms.
This release is source-available before its Change Date, not OSI open source.

Each version converts to Apache License 2.0 on the second anniversary of that
version's first public distribution by 49 Agents LLC. `RELEASE-LICENSES.json`
records public release dates once they exist. Its empty release list denotes
that no public release has been recorded; a private build date is not a public
release date. Existing versions' conversion dates must never be postponed.

Third-party packages, vendored assets and separately licensed components retain
their own copyrights and licenses. Qorqbot's production-use restriction does
not apply to those components independently. In particular:

- `qorqut-code/biz-dashboard/js/xterm.js` and `qorqut-code/biz-dashboard/css/xterm.css` retain their MIT
  terms; see `qorqut-code/biz-dashboard/THIRD-PARTY-NOTICES.md`.
- Python dependencies carry their own license files in their wheel metadata.
  Certifi is MPL-2.0; its Python and certificate source files are included in its
  wheel. Preserve their MPL rights and notices.
- The optional WhatsApp native helper and its linked dependencies need separate
  treatment; see `qorqut-code/extensions/whatsapp/LICENSE-SCOPE.md`. Do not infer BSL coverage
  from the adapter's containing directory.
- User-installed browsers, agent clients and operating-system tools are not
  relicensed or redistributed by Qorqbot's application license.

First-party component directories repeat the license and the Apache Change
License so independently built packages retain these terms. The packaged
license is authoritative; a short SPDX identifier alone omits the production
grant and conversion rule.
