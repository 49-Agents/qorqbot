# File-reference cards: Manager acceptance

2026-10-09 UTC. Candidate `52c21d0850102e348e3d13b7c781a47a63d223bc`,
base `15f8d50c3a3cee2c68671e7111160f140bebb4ba`, implements
[D47](../FILE-REFERENCE-CARDS.md) against accepted core
`c13eb8b2dbf023a69e84cde2eda0368058485e6f`. The complete production diff,
focused fixture/check changes and [builder evidence](file-reference-cards-r1.md)
were reviewed directly by Manager. The candidate was clean and its remote branch
matched the reported SHA. No new dependencies or Store/Terminal behavior changed.

Manager independently ran `npm run build` and
`npm test -- --grep 'file-reference cards|history paging anchors'` at the candidate.
Both Chrome groups passed (26.7 seconds total). Manager inspected the resulting
1440×960 desktop and 390×844 phone screenshots: cards wrap, paths remain selectable,
focus is visible and clipboard failure guidance remains readable. This host's
font lacks some emoji glyphs; exact Unicode path bytes remain intact. The group
also checks historical provenance, malformed content fallback, escaping, exact
copy bytes, precision limits, normal refresh/reload deduplication and absence of
file-content requests. No broad Terminal rerun was necessary.

Original Manager logs, screenshots and command results are under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manager-review296/`.
These use synthetic HTTP peers and a clipboard stub. They establish presentation,
not live filesystem availability, OS clipboard permission or core/MCP publication.
The next check pairs the compiled UI with the real temporary core server and
official MCP client. File opening remains deferred. No deployment or native model
call occurred. Acceptance covers this UI increment, not the complete product.
