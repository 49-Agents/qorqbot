# Monorepo migration verification

The new repository imports these source main branches:

| Module | Original tip | Imported commits |
| --- | --- | ---: |
| core | `ca00e5bf8d8b0c42469aee51bc7a1e42f60b6ca9` | 253 |
| browser | `6ba2831aa52cf4927e5aeb9ff217c7dccb8c164c` | 11 |
| ui | `e4d5ebafead8987ac4477348cf45120e7130e1ef` | 60 |

All 324 imported commits retain their original authors, committers, messages and
parent relationships. Their original trees match exactly beneath the new module
prefixes. One migration merge joins those histories and adds the shared README,
documentation, navigation updates and date checks. Both timestamp fields use
October 5–9, 2026, 07:05–08:59 in San Juan, as requested by the owner.

Original repositories and private bundles preserve the unmodified histories.
Commit IDs and one signature change as documented in the [migration decision](MONOREPO.md).

## Checks in the new layout

- Built and installed core and browser wheels independently with Python 3.12,
  using their existing dependency locks; both passed `pip check` and CLI help.
- Core's existing foundation suite: 26 tests passed against the installed wheel.
- Browser's existing service configuration suite: 3 tests passed against the
  installed wheel.
- UI: `npm ci`, TypeScript checking and the Vite production build passed. Vite
  retains its existing advisory about a JavaScript chunk over 500 kB.
- Verified imported trees, identities, messages, merge topology, parent ordering
  and both author/committer dates for every imported commit.
- Checked 16 date-hook cases covering both timestamp fields, both weekday
  boundaries, weekends and a timestamp expressed in a different UTC offset.
- Scanned 1,350 unique imported blobs (21,883,729 bytes) for common credential
  patterns; no matches. This is a bounded pattern check, not a security audit.
- Runtime source, dependency manifests/locks and existing tests retain their
  exact original bytes; changes within modules are documentation only.

No existing service, user session, browser profile or application database was
changed. These are migration/build checks, not renewed end-to-end or VM acceptance.
