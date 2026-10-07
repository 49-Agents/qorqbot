# Chat domain Manager acceptance

Accepted candidate: `5a79f480b448c1eb3d4172e6e83947146015283d`, based on
`055cbd48071d28c03cde3daf1a08521875c130a3`. Acceptance covers stored messages,
handling receipts, conversation reads and dated transcript publication through
the existing file/job protocol. It does not prove native assistant capture or
automatic publication in the combined running server.

Independent Astra MAX review 130 found no remaining in-scope defects. All 22
compact final-source domain checks and four installed checks passed. The latter
exercise the corrected publication recovery paths and real isolated HTTP/MCP
discovery/send/list/ack behavior. Revision checks retain unresolved errors for
changed files/ownership, enforce recovery holds, retry the owning-parent sync,
and recover a post-commit error without changing bytes, identity or receipts.

Manager inspected the complete production domain, models, migration/assembly
changes and the publication corrections. All 142 tracked files and all 50
installed Python files were independently compared with candidate Git bytes and
the review wheel. All 29 installed runtime pins match the lock; the two build
pins belong to the isolated build environment. The review wheel's SHA-256 is
`c1dba7314ed9046dbdf82ecd9ad09e3b8be1dbb7b9bfb7437c884bf1520bcfef`.

Manager reused that verified installed package and staged tests from candidate
Git bytes. Two focused checks passed in 4.112 seconds: atomic exact-body send
and receipt replay, and canonical dated entries for both endpoint agents and
onbash. Both original failure diagnostics then passed their corrected outcomes:
post-commit marker-read failure recovered to current/error-null after retry and
reopen, and failed owning-parent fsync was followed by a successful parent sync.
Each diagnostic retained one message without new chat. All 26 loaded product
modules resolved inside the verified installed package. Private evidence is
`manager-chat-r2/{verify.py,checks.log,diagnostics.log,result.json}` under the
existing private run directory; no Manager check failed.

The exact clean/pushed candidate has been combined with accepted console hosting
on the coordination branch as `e8d321dfe1ec1f399ab789cea7979f0b3b1ac890`.
Every merged path was verified as the unchanged version from one of the two
accepted inputs. Main remains at the accepted console baseline while the
combined integration checks are prepared. The worker fixture's synthetic handler
names now collide with actual chat handlers; this is an integration-test repair,
not permission to alter the fixed production registry.

No actual power loss, deployment, provider turn, terminal dispatch, native reply,
VM, browser/noVNC, full export/restore or whole G0–G6 acceptance is claimed.
The running-server and real-frontend checks remain necessary next steps.
