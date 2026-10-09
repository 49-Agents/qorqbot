# D53 core inbox reference view

Implemented against frozen plan/contract `9685beefaa0ccbd72fdd4eee46d1e49eac9270dc`
on `codex/build-inbox-reference-view-r1`. Manager acceptance is separate.

`ConversationItem` adds nullable `inbox_reference` with UUID `message_id` and
`message_entry_id`. The SQL-only read helper requires an exact canonical pointer,
native user role, part zero with no sibling projected part, the recipient's saved
message entry, and a submitted attempt validated through the existing saved-target
and runner-receipt validators. It checks the exact attempt run/source/epoch link,
archive and admitted owner bindings, provider, native UUID and terminal. A resumed
source can match a later linked run without changing the entry's first run ID.
Malformed, uncertain, unlinked and conflicted evidence yields null.

Matching uses this page's candidate message IDs and indexed SQL lookups; the saved
query plans contain no unrelated history scan. It does not choose a causal attempt
or prove automated authorship or handling. Identical manually typed pointers can
receive the same reference and remain separate, inspectable native entries.

Entry/NativeSource, parser/version, persistence, raw bytes, dated JSONL and page
identity/order/cursors are unchanged. No migration, dependencies, services or public
operations were added. No UI files or original completed evidence were modified.

## Focused verification

With the existing private `chat-test-venv/bin/python`, `PYTHONDONTWRITEBYTECODE=1`
and `PYTHONPATH=src:tests`:

```text
python -m unittest conversation.test_inbox_reference conversation.test_ledger \
  conversation.test_native_projection messaging.test_dispatch -v
Ran 21 tests in 40.850s — OK
git diff --check — clean
```

The two new grouped tests cover exact/ordinary/multipart text, missing/wrong message
and recipient, damaged targets/hashes/receipts, uncertain/deferred attempts, archive
and owner binding mismatches, conflict, off-page siblings, multiple valid attempts,
page-bounded validation and the exact later run link for a resumed source. The
preservation test uses Claude-shaped records, compares stored entries/provenance,
archive inventory/raw bytes and dated bytes before/after annotation, closes the
owner and reopens core, and verifies stable SQL-only reads. Codex-shaped records
cover matching. All data/process observations/receipts are synthetic temporary
fixtures; no native CLI, provider or browser was rerun.

Private evidence directory:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-inbox-reference-view-r1`.
It contains `focused-tests.log`, `bounded-sql-plans.json`, and a validated
`conversation-page.synthetic.json` with one positive and two null annotations.
`conversation-page-provenance.json` records its synthetic origin and SHA-256
`2565b3159b930ce92d2051f39307062985ca1e41351ef87a9f53151da2c5e746`.
The paired core/UI contract check and final approval belong to Manager; this
evidence claims no new native workflow, merge or deployment acceptance.
