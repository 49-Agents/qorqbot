# License scope

Copyright (c) 49 Agents LLC. Qorqbot's first-party code, documentation, agent
instructions and connector adapters are licensed under the PolyForm
Noncommercial License 1.0.0 in `LICENSE`, unless a file or component explicitly
carries another license. Qorqbot is source-available, not
OSI open source.

## Personal and noncommercial use

Personal use and other noncommercial use are free under `LICENSE`. This includes
personal projects, study, research, hobby use and use by the noncommercial
organizations the license names.

## Commercial use

Commercial use needs a commercial license from 49 Agents LLC. Commercial use
includes use by or for a company or other legal entity, other than the
noncommercial organizations `LICENSE` names, and any use in the course of
business, including work for clients. The commercial license is per
person: each person who uses Qorqbot for commercial purposes needs a seat.
Agents do not count as seats.

Commercial licenses are free until 2027-01-01. Terms and prices are at
<https://qorqbot.com/commercial-license/> and <https://qorqbot.com/pricing/>.

## Releases

`RELEASE-LICENSES.json` records each public release and its license. A
release's `first_public_distribution_date` stays empty until that version is
publicly distributed; a private build date is not a release date.
`LICENSES/Apache-2.0.txt` is kept for the separately licensed Apache 2.0
components below.

## Separately licensed components

Third-party packages, vendored assets and separately licensed components retain
their own copyrights and licenses. Qorqbot's noncommercial condition does not
apply to those components independently. In particular:

- `biz-dashboard/js/xterm.js` and `biz-dashboard/css/xterm.css` retain their MIT
  terms; see `biz-dashboard/THIRD-PARTY-NOTICES.md`.
- Python dependencies carry their own license files in their wheel metadata.
  Certifi is MPL-2.0; its Python and certificate source files are included in its
  wheel. Preserve their MPL rights and notices.
- The optional WhatsApp Python adapter, guidance and UI use Apache 2.0. Its
  independent native helper uses GPL-3.0-only with matching corresponding source;
  see `extensions/whatsapp/LICENSE-SCOPE.md`. These separately licensed programs
  are excluded from the application's PolyForm Noncommercial grant.
- The macOS app's Electron and Chromium notices ship with the app bundle.
- User-installed browsers, agent clients and operating-system tools are not
  relicensed or redistributed by Qorqbot's application license.

First-party component directories repeat `LICENSE` and `RELEASE-LICENSES.json`
so independently built packages retain these terms. The packaged license is
authoritative.
