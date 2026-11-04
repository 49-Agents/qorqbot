# Overseer review helper

`tools/overseer_review.py` is a standalone, stdlib-only helper for the extraction step that reads episodes and then
records coverage. It reads local JSON files and writes local files. It holds no credentials, calls no service and adds no
tool, schedule or worker. You still call the existing tools yourself (`overseer_evidence_list`, `overseer_episode_get`,
`overseer.proposals.record`, `overseer.coverage.record`, `workflows.complete_step`).

It replaces shortened briefs and automatic checkpoint builders. It cannot tell whether you read anything: the
`reviewed` list you give it is your attestation, so make it true.

## Supported workflow

1. **Fetch.** With the admitted extraction `run_id`, call `overseer_evidence_list`, then `overseer_episode_get` with the same
   `run_id`, the episode ref and the frozen `frontier`. Repeat with each returned `next_cursor` until it is `null`. On
   `evidence_busy`, retry the identical request. If a response has `cursor_reset: true`, restart that episode from the page
   returned and do not combine it with older pages. Save every response exactly as returned, unedited, one file per page, in
   fetch order, inside the request envelope `{"request_cursor": <the cursor you sent, or null for the first page>,
   "response": <the response>}`. The envelope is required for new fetching: it is the only way the helper can prove no page was
   skipped. Fragment hashes only prove the sources that were supplied, not a source on a page you never saved. Bare pages
   (older files) still assemble and verify individual sources, but their episode chain is reported `unverified`, never
   complete. A page without an explicit `next_cursor` (null or a string) is malformed, not final.
2. **Assemble.** `python3 tools/overseer_review.py assemble --out DIR PAGE.json ...` (`--replace` rewrites a previous
   batch; `-` reads stdin). Each source is rebuilt from its fragments by exact identity (provider, kind, namespace, record
   ID, revision, content hash). It must have offsets contiguous from zero, one coherent reference per identity (including
   `material_ref`), `json` encoding, valid JSON and a SHA-256 equal to its `content_hash`. Identical duplicate fragments and
   a source delivered on several episodes count once. Output: `DIR/page-NNNN.txt` and `DIR/manifest.json`. Exit status is 0
   when nothing is inconsistent, 1 when `errors` is not empty, 2 when the request itself is refused.
3. **Check the result.** In the printed summary:
   - `pending` sources lack their later pages. Fetch them and run `assemble --replace` again with all pages.
   - `errors` are rejected sources (missing, conflicting or overlapping fragments, wrong hash, bad encoding, incoherent
     reference) and broken chains (skipped page, page after the final one, malformed page). Nothing is filled in or trimmed;
     rejected sources get no page text and cannot be attested.
   - `episodes_not_proven_complete` lists each episode whose chain is not `complete`: `unfinished` (the last page still has a
     `next_cursor`), `unverified` (explicit null at the end but no proven request-cursor chain) or `broken`. Only `complete`
     (first page requested without a cursor or a valid `cursor_reset`, every request cursor equal to the previous
     `next_cursor`, explicit final null) counts toward `fetch_complete`. Fully assembled sources in a partial episode can
     still be attested; sources on missing pages are simply not known, and nothing is invented for them.
4. **Read.** Read every file in `read_every_page`, completely. Content lines start with `| `, are at most 1,000 characters
   (`--line-chars`) and, when joined without a separator, give the exact canonical JSON text. Each part header shows its
   source, `chars start-end of total` and a slice SHA-256; every page ends with an `END OF PAGE` line, so a view that stops
   early is visible. Do not pipe pages through `cut`, `head`, `grep` or any summary. A summary may help you navigate but
   never replaces reading a source.
5. **Record outputs first.** Commit the proposals (`overseer.proposals.record`) and any owner clarifications
   (`overseer_input_request`) for what you read, before any coverage.
6. **Attest.** Write `reviewed.json`, a list of the exact `ref` objects from `manifest.json` of sources whose every part you read
   and analyzed. Only those are attested; everything else in the batch becomes `pending_sources`. Optionally write
   `unresolved.json`: `[{"source_ref": <exact ref>, "reason": "<why it could not be read>"}]`. The ref may be an original saved
   exact reference that this batch could not deliver (for example a forbidden or empty episode response); give its full
   original ref. It is validated like any source reference, kept exactly as supplied, added to `pending_sources` with one
   structured `unresolved_source` gap, and never to `analyzed_sources`. A ref that is in the batch must match the batch's
   metadata, and a source cannot be both reviewed and unresolved. Reviewed refs must always be in the batch. Then:
   `python3 tools/overseer_review.py coverage DIR --run-id RUN --reviewed reviewed.json [--unresolved unresolved.json]
   [--next-cursor CURSOR]`.
   It refuses the whole request when a reviewed ref is not in the batch, differs from the batch's reference (stale revision or
   hash), or is pending or rejected. With no `--reviewed`, `analyzed_sources` is empty. Fetching, a null cursor and a successful
   assemble never add to it.
7. **Record.** Send each emitted `{"operation", "arguments"}` to the existing `overseer.coverage.record` unchanged (the
   `operation_id` is derived from the record, so a retry is a replay). More than 200 references are split into several
   requests in a fixed order with none dropped; only the last can be `complete`, and it carries `--next-cursor`.

## Three different states

- **Fetch complete:** every episode chain is proven `complete` and every source assembled (`summary.fetch_complete`).
- **Reviewed:** the sources you listed in `reviewed.json`, recorded as `analyzed_sources`.
- **Workflow finished:** a separate fact. See "Finishing" below.

## Gaps

A coverage `gaps` list holds only: (a) the gaps the episode responses themselves reported for this batch, kept as received,
deduplicated and sorted; (b) one `{"code": "unresolved_source", "source": <identity>, "reason": ...}` for each source in
`unresolved.json`. It never holds progress notes, `coverage_partition`, `analysis_record` or old receipts' gaps.
`related_episodes` are optional navigation, shown only as counts per episode; an uncaptured one adds no gap and never makes a
batch incomplete. Keep progress in the run's notes or the step `completion_note`.

The frontier's own gaps are real scan limitations (for example a source the inventory could not read). The helper keeps them
in each manifest as `inventory_gaps` but does not copy them into every per-batch receipt. They must not vanish when a batch
is done: before closing the run, collect `inventory_gaps` from every batch's manifest, deduplicate them, and include the
result, together with the unresolved, pending and rejected sources, in the final reconciliation and in the
`completion_note`/`output` of the closing step:

```
python3 - BATCH_DIR_1 BATCH_DIR_2 ... <<'PY'
import json, sys
seen = {}
for directory in sys.argv[1:]:
    for gap in json.load(open(directory + "/manifest.json"))["inventory_gaps"]:
        seen[json.dumps(gap, sort_keys=True)] = gap
print(json.dumps([seen[key] for key in sorted(seen)], indent=2))
PY
```

`complete` is true only when every episode chain is finished, nothing was rejected, every source in the batch was attested,
and there is no gap, pending source or `--next-cursor`. It describes this batch only.

Pages contain private source text. Keep `DIR` in your own scratch space, never in a repository, and remove it when the batch
is recorded.

## Finishing the workflow

Close the run only after the scanner has drained (a null inventory cursor with no runnable continuation) and review has
really finished. The final output keeps the deduplicated `inventory_gaps` of all batches and the unresolved sources. Use the
existing step operations, in dependency order (`resolve-frontier`, `gather-episodes`,
`inspect-and-record`, `coverage`): `workflows.complete_step` with `run_id`, `step_id`, a new `operation_id` and a
`completion_note`/`output` giving fully reviewed exact sources, proposals and the real limitations. The run finishes when
its last step completes. If a technical blocker remains, leave the steps open and say so instead of reporting completion.
A bounded extraction that ends with documented unavailable material is finished with limitations, which is different from
complete source coverage.

## Draft Overseer instruction text (not activated; for supported authoring later)

> **Review and coverage helper.** For extraction, read episodes with `overseer_episode_get` until `next_cursor` is null,
> save each exact response with the cursor you sent in a `request_cursor` envelope, and run
> `tools/overseer_review.py assemble`. Read every rendered page completely; never shorten,
> filter or summarize source text and never rely on a fetch finishing. A fragment you have not read leaves its source
> pending. Record proposals first, then list only the exact sources you fully read and analyzed in `reviewed.json` and use
> `tools/overseer_review.py coverage` to build the `overseer.coverage.record` arguments. Gaps are only real evidence
> limitations the tools reported or sources you could not read, with their structured references; progress notes, optional
> related-episode links and old gap lists are not gaps. Finish a run only through `workflows.complete_step`, after the
> scanner has drained and review is actually done, and report fully reviewed sources, proposals and remaining limitations
> accurately, including the scan limitations the inventory reported. Receipts are immutable: correct an earlier claim by appending a new receipt after actually reading the
> material, never by reset or edit, and report anything you cannot verify as unverified.

## Limits

Offsets are character positions in the canonical JSON text (`sort_keys`, compact separators, `ensure_ascii=False`). One batch
covers one frontier snapshot; inputs from different snapshots are refused, so assemble each separately. The helper checks
integrity and consistency, not whether a source is relevant, and it cannot confirm that anything was read. It cannot know of a
source on a page that was never saved unless the request-cursor chain shows the page is missing.
