#!/usr/bin/env python3
"""Generates tests/fixtures/context-source/exchanges.json and captured.json for the H2-ui browser checks.

Two products, kept apart on purpose:

* captured.json - a bounded, VERBATIM subset of the host's committed captures
  (app/tests/fixtures/context_source_status/captures.json at the pinned host commit). Nothing is edited or re-normalized.
* exchanges.json - exchanges GENERATED through the same fixed host handlers (the host's own composed test host, its real HTTPAPI
  dispatcher and its own Normalizer) for the exact requests this dashboard sends: 6000-character pages, 100-character preflights,
  limit 30/25, one-key control bodies. The host's capture uses different page sizes and limits, so those requests cannot be
  taken from it. Every exchange is stamped with the stage (a named host state) it was recorded in.

This tool only IMPORTS the pinned host, Operations and Context sources from read-only archives and runs them in a temporary
directory. It never edits them and never reads operational data. Usage (see docs/CONTEXT-SOURCE-UI.md):

    PINS=/tmp/h2pins  # `git archive` of the three pinned commits into host/ ops/ ctx/
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PINS/host/app:$PINS/host/app/tests:$PINS/ops:$PINS/ctx \\
      QORQUT_TEST_WORKSPACE_ROOT=<v1.2 workspace root> QORQUT_TEST_TEMPLATE_ROOT=<biz-template checkout> \\
      <python with starlette> generate_exchanges.py --pins $PINS
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

SHAS = {
    "host": "4f96d823c8d0b1ea8b5f3935d114b79ad1e8356b",
    "operations": "2d697fa3973e929f7f2be5afa5e9a731db634aaa",
    "context": "32431e125b1982ea10fce4db1993ab22c6a241be",
}
HERE = Path(__file__).parent
QUICK = "--quick" in sys.argv

# Captures of the host fixture this dashboard keeps verbatim: the write commands (their request shape equals the dashboard's apart from
# the idempotency key), the Global source commands, the foreign-scope refusals and the process/queue reads.
CAPTURED_NAMES = (
    "upload/accepted", "pilot/queued/queue-get", "pilot/blocked/queue-get", "pilot/blocked/processes", "pilot/blocked/processes-global",
    "pilot/blocked/processes-other-namespace", "pilot/retry-stale-revision", "pilot/retry-pending/queue-get", "pilot/published/processes",
    "pilot/applied/processes", "pilot/diagnostics-filings", "pilot/diagnostics-map", "pilot/retract-stale-revision", "pilot/retract",
    "private/discard", "private/discard-requested/queue-get", "conflict/processes", "note/submitted-queued", "journal/submitted-queued",
    "journal/edited-after-submission", "global/queue-waiting-behind-blocked", "global/retract-with-force-refused",
    "global/retract-stale-revision", "global/retract", "global/processes-wrong-view", "long-business/page-1",
    "long-business/body-change", "long-business/page-2-after-body-change", "global-ops/queued/queue-get",
    "global-ops/blocked/queue-get", "global-ops/blocked/queue-retry-wrong-state", "global-ops/blocked/queue-retry",
    "global-ops/retry-pending/queue-get", "global-ops/retry-pending/queue-retry-refused", "global-ops/retry-pending/queue-discard-refused",
    "global-ops/retry-pending/queue-retry-replay", "global-ops/published/queue-get", "global-ops/second/queue-discard-never-started",
    "global-ops/second/queue-get-discarded", "global-ops/applied/retract-stale-revision", "global-ops/applied/retract",
    "global-ops/applied/retract-replay", "global-ops/third/retry-stale-revision", "global-ops/third/retry",
    "global-ops/third/retry-replay-after-source-moved", "global-ops/foreign/get-business-source", "global-ops/foreign/retry-business-source",
    "global-ops/foreign/retract-business-source", "global-ops/foreign/queue-get-business-input", "global-ops/foreign/queue-retry-business-input",
    "global-ops/foreign/queue-discard-business-input", "global-ops/foreign/queue-get-global-input-from-business",
    "global-ops/foreign/retry-global-source-from-business", "global-ops/foreign/retract-global-source-from-business",
    "global-ops/selector-refused/business_id", "global-ops/selector-refused/review_business_id", "global-ops/selector-refused/queue-get",
    "global-ops/list-page-1", "global-ops/list-page-2", "global-ops/list-cursor-from-another-query",
)


def captured(pins: Path, out: Path) -> dict:
    document = json.loads((pins / "host/app/tests/fixtures/context_source_status/captures.json").read_text())
    by_name = {entry["name"]: entry for entry in document["captures"]}
    missing = [name for name in CAPTURED_NAMES if name not in by_name]
    assert not missing, missing
    chosen = [by_name[name] for name in CAPTURED_NAMES]
    assert all(entry["transport"] == "http-dispatcher" for entry in chosen), "only HTTP-dispatcher captures are shipped as HTTP evidence"
    result = {"provenance": {"status": "captured", "source": "app/tests/fixtures/context_source_status/captures.json",
                             "host_commit": SHAS["host"], "operations_commit": SHAS["operations"], "context_commit": SHAS["context"],
                             "host_provenance": document["provenance"], "limitations": document["limitations"],
                             "normalization": document["normalization"],
                             "selection": f"{len(chosen)} of {len(document['captures'])} captures, verbatim; "
                                          "transports: http-dispatcher only (the ten application-invoke captures and the one "
                                          "route-handler capture are agent evidence, not HTTP, and are not shipped)"},
              "captures": chosen}
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    return result


def generated(out: Path) -> dict:
    import context_source_status_capture as host_capture
    from context_source_status_capture import Normalizer, deterministic_entropy, start
    from context_source_status_support import Http, settle

    entries: list[dict] = []
    stage = ["start"]

    def call(http, method, path, body=None, query=None, *, record=True):
        status, payload, _ = http.call(method, path, body, query)
        if record:
            entries.append({"stage": stage[0], "request": {"method": method, "path": "/api/" + path, "query": query or {}, "body": body},
                            "response": {"status": status, "body": payload}})
        return status, payload

    def op(http, slug, name, body, record=True):
        return call(http, "POST", f"biz/{slug}/operations/{name}" if slug else f"root/operations/{name}", body, record=record)

    def read_doc(http, slug, doc_id, *, queue=True):
        """What the source reader sends: every 6000-character page, the 100-character status read, the diagnostics and the input."""
        scope = "business" if slug else "shared"
        status, data = op(http, slug, "context_get", {"scope": scope, "document_id": doc_id, "max_chars": 6000})
        for _ in range(8):
            cursor = ((data or {}).get("data") or {}).get("next_cursor")
            if status != 200 or not cursor:
                break
            status, data = op(http, slug, "context_get", {"scope": scope, "document_id": doc_id, "max_chars": 6000, "cursor": cursor})
        status, head = op(http, slug, "context_get", {"scope": scope, "document_id": doc_id, "max_chars": 100})
        op(http, slug, "context_upload_map", {"upload_id": doc_id})
        input_id = (((head or {}).get("data") or {}).get("document") or {}).get("filing", {}).get("input_id")
        if input_id and queue:
            op(http, slug, "context_queue_get", {"input_id": input_id})

    def processes(http):
        call(http, "GET", "biz/first/context", query={"view": "processes"})
        call(http, "GET", "root/context", query={"view": "processes"})

    def snapshot(http, name, *, business=(), root=(), lists=False, root_lists=False, no_queue=()):
        stage[0] = name
        for doc_id in business:
            read_doc(http, "first", doc_id, queue=doc_id not in no_queue)
        for doc_id in root:
            read_doc(http, None, doc_id)
        processes(http)
        if lists:
            for kind in ("context_upload", "context_dump"):
                status, page = op(http, "first", "context_list", {"scope": "business", "kind": kind, "limit": 30})
                cursor = ((page or {}).get("data") or {}).get("next_cursor")
                while cursor:
                    status, page = op(http, "first", "context_list", {"scope": "business", "kind": kind, "limit": 30, "cursor": cursor})
                    cursor = ((page or {}).get("data") or {}).get("next_cursor")
            op(http, "first", "context_filings", {"limit": 25})
        if lists or root_lists:
            for kind in ("context_note", "context_topic", "context_person", "context_upload", "context_dump"):
                op(http, None, "context_list", {"scope": "shared", "kind": kind, "limit": 30})

    def write(http, slug, name, body, key, **options):
        return op(http, slug, name, {**body, "idempotency_key": key, **options})

    with deterministic_entropy(), tempfile.TemporaryDirectory() as tmp:
        host = start(Path(tmp) / "ui")
        http = Http(host)
        owner, root = host.human(), host.human(None)
        step = host.step

        def settle_job(job):
            settle(host, job)

        def upload(path, body, topic):
            status, accepted = call(http, "POST", path, {"text": body, "topic": topic}, record=False)
            assert status in (200, 202), accepted
            return accepted

        def drain(doc_id, job, who=owner):
            for _ in range(12):
                if host.read(who, "context_get", {"document_id": doc_id})["document"]["filing"]["state"] == "published":
                    break
                step()
            settle_job(job)

        def doc(who, doc_id):
            return host.read(who, "context_get", {"document_id": doc_id})["document"]

        # --- 46 filed uploads, so the Uploads list needs a second page (30 per page) ---------------------------------------------
        filed = []
        for number in range(2 if QUICK else 46):
            accepted = upload("biz/first/context/upload", f"Filed history note {number:02d}.", f"Filed history {number:02d}")
            drain(accepted["document_id"], accepted["job"]["id"]); filed.append(accepted["document_id"])

        # --- a long source with markup and non-ASCII text (several 6000-character pages) --------------------------------------------
        long_body = "Opening paragraph with <img src=x onerror=\"window.__xss=1\"> and **markup** left as text.\n" + "".join(
            f"Long line {n:03d} with accents éü and 日本語 text.\n" for n in range(240))
        long_source = upload("biz/first/context/upload", long_body, "Long source")
        snapshot(http, "long-queued", business=[long_source["document_id"]], no_queue=[long_source["document_id"]])
        drain(long_source["document_id"], long_source["job"]["id"])
        snapshot(http, "long-filed", business=[long_source["document_id"]], no_queue=[long_source["document_id"]])
        edited_source = upload("biz/first/context/upload", long_body, "Long source, edited")
        drain(edited_source["document_id"], edited_source["job"]["id"])
        stage[0] = "long-edited"
        status, first_page = op(http, "first", "context_get", {"scope": "business", "document_id": edited_source["document_id"], "max_chars": 6000})
        old_cursor = first_page["data"]["next_cursor"]
        current = doc(owner, edited_source["document_id"])
        host.data(owner, "context_document_update", {"document_id": edited_source["document_id"], "body": long_body + "One more line.\n",
                                                     "expected_content_hash": current["content_hash"]}, expected_revision=current["revision"])
        op(http, "first", "context_get", {"scope": "business", "document_id": edited_source["document_id"], "max_chars": 6000, "cursor": old_cursor})
        read_doc(http, "first", edited_source["document_id"], queue=False)
        processes(http)
        step(); settle_job(doc(owner, edited_source["document_id"])["filing"]["job_id"])

        # --- the pilot upload through every state --------------------------------------------------------------------------
        accepted = upload("biz/first/context/upload", "Met Alice about the pilot.", "Pilot call")
        pilot, pilot_job = accepted["document_id"], accepted["job"]["id"]
        snapshot(http, "pilot-queued", business=[pilot])
        seen = {}

        def during(tools, payload):
            seen["now"] = True
            stage[0] = "pilot-processing"
            read_doc(http, "first", pilot)
            processes(http)
        host.runner.during = during
        host.runner.fail = RuntimeError("model unavailable")
        step()
        host.runner.during = None
        assert seen, "the processing snapshot did not run"
        snapshot(http, "pilot-blocked", business=[pilot])
        blocked_revision = doc(owner, pilot)["revision"]
        host.runner.fail = None
        stage[0] = "pilot-blocked"
        write(http, "first", "context_retry", {"upload_id": pilot}, "ui-pilot-retry", expected_revision=blocked_revision)
        write(http, "first", "context_retry", {"upload_id": pilot}, "ui-pilot-retry", expected_revision=blocked_revision)   # same key replayed
        snapshot(http, "pilot-retry-pending", business=[pilot])
        step()
        snapshot(http, "pilot-completion-pending", business=[pilot])
        settle_job(pilot_job)
        snapshot(http, "pilot-applied", business=[pilot])
        applied_revision = doc(owner, pilot)["revision"]
        stage[0] = "pilot-applied"
        write(http, "first", "context_retry", {"upload_id": pilot}, "ui-pilot-retry-stale", expected_revision=blocked_revision)   # the revision the blocked page showed
        stage[0] = "pilot-applied"
        status, removal = write(http, "first", "context_retract", {"upload_id": pilot}, "ui-pilot-retract", expected_revision=applied_revision)
        snapshot(http, "pilot-retract-pending", business=[pilot])
        step(); settle_job(removal["data"]["job"]["id"])
        snapshot(http, "pilot-retracted", business=[pilot])
        stage[0] = "pilot-retracted"
        write(http, "first", "context_retract", {"upload_id": pilot}, "ui-pilot-retract-stale", expected_revision=applied_revision)   # the revision the filed page showed
        retracted = doc(owner, pilot)
        host.data(owner, "context_document_update", {"document_id": pilot, "body": "Newer body about the pilot.",
                                                     "expected_content_hash": retracted["content_hash"]}, expected_revision=retracted["revision"])
        snapshot(http, "pilot-changed-after-publication", business=[pilot])

        # --- a private owner upload: a blocked discard is a pending request, then discarded ----------------------------------
        secret = upload("biz/first/context/upload", "Private discussion with Carol.", "Private")
        secret_id, secret_job = secret["document_id"], secret["job"]["id"]
        host.runner.fail = RuntimeError("a private provider detail")
        step()
        snapshot(http, "private-blocked", business=[secret_id])
        input_id = doc(owner, secret_id)["filing"]["input_id"]
        stage[0] = "private-blocked"
        write(http, "first", "context_queue_discard", {"input_id": input_id, "expected_state": "blocked"}, "ui-private-discard")
        snapshot(http, "private-discard-requested", business=[secret_id])
        step(); settle_job(secret_job)
        snapshot(http, "private-discarded", business=[secret_id])
        host.runner.fail = None

        # --- a stopped job after publication (completion conflict) ---------------------------------------------------------------
        conflict = upload("biz/first/context/upload", "Second discussion with Dave.", "Conflict")
        step(); host_capture.cancel(host, conflict["job"]["id"]); step()
        snapshot(http, "conflict", business=[conflict["document_id"]])

        # --- a withdrawn upload ---------------------------------------------------------------------------------------------
        stage[0] = "withdrawn-retract"
        withdrawn = upload("biz/first/context/upload", "A note that will be withdrawn.", "Withdrawn")
        drain(withdrawn["document_id"], withdrawn["job"]["id"])
        status, removal = write(http, "first", "context_retract", {"upload_id": withdrawn["document_id"]}, "ui-withdrawn",
                                expected_revision=doc(owner, withdrawn["document_id"])["revision"])
        step(); settle_job(removal["data"]["job"]["id"])
        snapshot(http, "withdrawn", business=[withdrawn["document_id"]])

        # --- notes: a business note and two journal entries through blocked, retried, filed and edited -------------------------------
        def note_now(path, note_id):
            status, rows = call(http, "GET", path, query=({"category": "strategy-notes"} if path.startswith("biz/") else None), record=False)
            return next(item for item in rows if item["id"] == note_id)

        def note_row(title, body):
            created = host.data(owner, "notes_create", {"title": title, "body": body, "category": "strategy-notes", "target_business_id": host.ids["first"]})["document"]
            return created

        def submit_note(created):
            host.data(owner, "notes_upload", {"document_id": created["id"], "expected_content_hash": created["content_hash"],
                                              "target_business_id": host.ids["first"]}, expected_revision=created["revision"])

        other = note_row("Other note", "A second note, filed first.")
        submit_note(other)
        status, other_rows = call(http, "GET", "biz/first/notes", query={"category": "strategy-notes"}, record=False)
        other_upload = next(item for item in other_rows if item["id"] == other["id"])["uploadId"]
        drain(other_upload, doc(owner, other_upload)["filing"]["job_id"])
        note = note_row("Strategy", "Plan the launch.")
        submit_note(note)
        stage[0] = "note-queued"
        call(http, "GET", "biz/first/notes", query={"category": "strategy-notes"})
        host.runner.fail = RuntimeError("model unavailable")
        step()
        stage[0] = "note-blocked"
        status, notes_blocked = call(http, "GET", "biz/first/notes", query={"category": "strategy-notes"})
        note_upload = next(item for item in notes_blocked if item["id"] == note["id"])["uploadId"]
        op(http, "first", "context_get", {"scope": "business", "document_id": note_upload, "max_chars": 100})
        op(http, "first", "context_get", {"scope": "business", "document_id": other_upload, "max_chars": 100})
        host.runner.fail = None
        stage[0] = "note-blocked"
        note_doc = doc(owner, note_upload)
        write(http, "first", "context_retry", {"upload_id": note_upload}, "ui-note-retry", expected_revision=note_doc["revision"])
        stage[0] = "note-retry-pending"
        call(http, "GET", "biz/first/notes", query={"category": "strategy-notes"})
        step(); drain(note_upload, note_doc["filing"]["job_id"])
        stage[0] = "note-filed"
        call(http, "GET", "biz/first/notes", query={"category": "strategy-notes"})
        current_note = note_now("biz/first/notes", note["id"])
        host.data(owner, "notes_update", {"document_id": note["id"], "body": "Plan the launch, then iterate.", "expected_content_hash": current_note["content_hash"],
                                          "target_business_id": host.ids["first"]}, expected_revision=current_note["revision"])
        stage[0] = "note-edited-after-submission"
        call(http, "GET", "biz/first/notes", query={"category": "strategy-notes"})

        def journal_entry(title, body):
            created = host.data(root, "notes_create", {"title": title, "body": body})["document"]
            host.data(root, "journal_upload_to_filer", {"document_id": created["id"], "expected_content_hash": created["content_hash"]},
                      expected_revision=created["revision"])
            return created

        def submission(created):
            status, rows = call(http, "GET", "root/notes", record=False)
            return next(item for item in rows if item["id"] == created["id"])["filerSubmissions"][0]["uploadId"]

        host.runner.fail = RuntimeError("model unavailable")
        blocked_journal = journal_entry("Daily blocked", "Today I met Frank.")
        step()
        stage[0] = "journal-blocked"
        call(http, "GET", "root/notes")
        blocked_upload = submission(blocked_journal)
        op(http, None, "context_get", {"scope": "shared", "document_id": blocked_upload, "max_chars": 100})
        host.runner.fail = None
        stage[0] = "journal-blocked"
        blocked_doc = host.read(root, "context_get", {"document_id": blocked_upload})["document"]
        write(http, None, "context_retry", {"upload_id": blocked_upload}, "ui-journal-retry", expected_revision=blocked_doc["revision"])
        step(); drain(blocked_upload, blocked_doc["filing"]["job_id"], root)
        filed_journal = journal_entry("Daily filed", "Today I met Grace.")
        filed_upload = submission(filed_journal)
        drain(filed_upload, host.read(root, "context_get", {"document_id": filed_upload})["document"]["filing"]["job_id"], root)
        current_journal = note_now("root/notes", filed_journal["id"])
        host.data(root, "notes_update", {"document_id": filed_journal["id"], "body": "Edited later.", "expected_content_hash": current_journal["content_hash"]},
                  expected_revision=current_journal["revision"])
        stage[0] = "journal-filed-and-edited"
        call(http, "GET", "root/notes")

        # --- Global sources -------------------------------------------------------------------------------------------------
        global_ids = []
        for number, text in enumerate(("A Global filed source.", "A Global queued source.")):
            accepted = upload("root/context/upload", text, f"Global {number}")
            global_ids.append((accepted["document_id"], accepted["job"]["id"]))
        snapshot(http, "global-queued", root=[g[0] for g in global_ids], root_lists=True)
        drain(global_ids[0][0], global_ids[0][1], root)
        snapshot(http, "global-first-filed", root=[g[0] for g in global_ids])
        revision = host.read(root, "context_get", {"document_id": global_ids[0][0]})["document"]["revision"]
        stage[0] = "global-first-filed"
        call(http, "POST", f"root/context-library/{global_ids[0][0]}/retract", {"force": True})
        status, global_removal = call(http, "POST", f"root/context-library/{global_ids[0][0]}/retract", {"expected_revision": revision})
        drain(global_ids[1][0], global_ids[1][1], root)
        for _ in range(6):                      # the withdrawal and everything else is filed before the queue is made to fail again
            step()
        settle_job(global_removal["job"]["id"]) if isinstance(global_removal, dict) and global_removal.get("job") else None
        for _ in range(6):
            step()

        # --- a blocked head with a pending input retry and a never-started queued input behind it ---------------------------------------
        host.runner.fail = RuntimeError("model unavailable")
        blocked = upload("biz/first/context/upload", "A source the model could not file.", "Blocked at the end")
        step()
        blocked_input = doc(owner, blocked["document_id"])["filing"]["input_id"]
        snapshot(http, "blocked-input", business=[blocked["document_id"]])
        stage[0] = "blocked-input"
        write(http, "first", "context_queue_retry", {"input_id": blocked_input, "expected_state": "blocked"}, "ui-input-retry")
        snapshot(http, "blocked-input-retry-pending", business=[blocked["document_id"]])
        queued = upload("biz/first/context/upload", "Still waiting in the queue.", "Queued at the end")
        queued_input = doc(owner, queued["document_id"])["filing"]["input_id"]
        snapshot(http, "final", business=[queued["document_id"], blocked["document_id"], pilot, secret_id, conflict["document_id"], withdrawn["document_id"],
                                          long_source["document_id"], filed[0]],
                 root=[g[0] for g in global_ids], lists=True, no_queue=[long_source["document_id"], filed[0]])
        stage[0] = "final"
        write(http, "first", "context_queue_discard", {"input_id": queued_input, "expected_state": "queued"}, "ui-queued-discard")
        snapshot(http, "final-queued-discarded", business=[queued["document_id"]])

        # --- a bounded history: 101 accepted revisions of one source (the diagnostics read returns the newest 100). An edit leaves the
        # --- source unfiled; filing the new revision is an explicit retry, which the host accepts while earlier inputs are still queued.
        many = upload("biz/first/context/upload", "Revision 1.\n", "Many revisions")
        for number in range(2, 4 if QUICK else 102):
            current = doc(owner, many["document_id"])
            host.data(owner, "context_document_update", {"document_id": many["document_id"], "body": f"Revision {number}.\n",
                                                         "expected_content_hash": current["content_hash"]}, expected_revision=current["revision"])
            edited_now = doc(owner, many["document_id"])
            host.data(owner, "context_retry", {"upload_id": many["document_id"]}, expected_revision=edited_now["revision"])
        stage[0] = "many-revisions"
        read_doc(http, "first", many["document_id"], queue=False)

    document = {"provenance": {
        "status": "generated-through-fixed-host-handlers", "host_commit": SHAS["host"], "operations_commit": SHAS["operations"], "context_commit": SHAS["context"],
        "generator": "tests/fixtures/context-source/generate_exchanges.py (imports the pinned host's context_source_status_capture and support modules)",
        "what": "The requests this dashboard actually sends (6000-character pages, 100-character preflights, limit 30 and 25), answered by the "
                "fixed host through its real HTTPAPI dispatcher over the host's composed synthetic test host with an injected model runner. "
                "Each exchange belongs to a named stage = one host state; responses are never edited to match another request.",
        "limitations": json.loads((Path(host_capture.__file__).parent / "fixtures/context_source_status/captures.json").read_text())["limitations"],
        "normalization": host_capture.NORMALIZATION},
        "exchanges": Normalizer().normalized(json.loads(json.dumps(entries, sort_keys=True, default=str)))}
    out.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pins", required=True, type=Path)
    parser.add_argument("--quick", action="store_true", help="dry run with a short history; writes nothing useful")
    args = parser.parse_args()
    captured(args.pins, HERE / "captured.json")
    result = generated(HERE / "exchanges.json")
    print(len(result["exchanges"]), "generated exchanges")
