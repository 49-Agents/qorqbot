from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import subprocess
import time

from starlette.testclient import TestClient

from qorqut_operations.contracts import InvocationContext, Principal, new_id
from qorqut_operations.documents.serialization import document_bytes
from bazarlyk.service.local import LocalFiles, _is_claude_transcript
from bazarlyk.storage import Database
from bazarlyk.transport.compatibility import DashboardCompatibility
from test_background_programs import Model
from test_service_transports import ORIGIN, ServiceFixture


def wait_job(client, service, identity):
    service.runtime.background.kick()
    for _ in range(200):
        read = client.get("/api/biz/widget/jobs/" + identity)
        assert read.status_code == 200, read.text
        job = read.json()["data"]["job"]
        if job["state"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.03)
    raise AssertionError("Job did not finish: " + json.dumps(job))


def test_registered_routine_runs_through_http_and_keeps_global_destination_explicit(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    subprocess.run(["git", "init", "--quiet", str(service.fixture.hq)], check=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    with TestClient(service.web) as client:
        service.login(client)
        view = client.get("/api/biz/widget/routines")
        assert view.status_code == 200, view.text
        assert any(program["id"] == "repository.status" for program in view.json()["programs"])
        definition = {"name": "Repository snapshot", "program": "repository.status", "arguments": {},
            "schedule": "0 9 * * *", "timezone": "UTC", "status": "paused"}
        created = client.post("/api/biz/widget/routines", json={"op": "add", "definition": definition}, headers=ORIGIN)
        assert created.status_code == 200, created.text
        routine = created.json()["data"]["records"][0]
        run = client.post("/api/biz/widget/routines", json={"op": "run", "id": routine["id"]}, headers=ORIGIN)
        assert run.status_code == 200, run.text
        job = wait_job(client, service, run.json()["data"]["job_ids"][0])
        assert job["state"] == "succeeded", job
        assert job["initiated_by_principal_id"] == service.fixture.owner.id
        assert job["principal_id"] != service.fixture.owner.id
        history = client.get("/api/biz/widget/routines/runs")
        assert history.status_code == 200, history.text
        assert history.json()[0]["ok"] is True
        assert client.get("/api/root/routines").status_code == 200
        assert client.get("/api/root/routines", params={"review_business_id": service.fixture.business_id}).status_code == 422
        assert client.get("/api/biz/widget/routines/runs?n=wrong").status_code == 422
        assert client.get("/api/routines/costs", params={"biz": "widget", "days": "wrong"}).status_code == 422
        forbidden = client.post("/api/biz/widget/routines", json={"op": "add", "run": "sh -c bad"}, headers=ORIGIN)
        assert forbidden.status_code == 422


def test_context_summary_uses_real_jobs_and_local_transcripts_are_registered(tmp_path):
    model = Model()
    service = ServiceFixture(tmp_path, full=True, model_client_factory=lambda business: model)
    with TestClient(service.web) as client:
        service.login(client)
        def operation(name, args):
            result = client.post("/api/biz/widget/operations/" + name, json={**args, "idempotency_key": new_id()}, headers=ORIGIN)
            assert result.status_code == 200, result.text
            return result.json()["data"]
        topic = operation("context_topics_create", {"title": "Customers", "slug": "customers"})["document"]
        note = operation("context_notes_create", {"topic_id": topic["id"], "title": "Pilot evidence", "body": "Ada needs a signed pilot."})["document"]
        model.answers.append("Ada needs a signed pilot.")
        queued = client.post("/api/biz/widget/context/summarize", json={"note": note["path"].removeprefix("context/")}, headers=ORIGIN)
        assert queued.status_code == 202, queued.text
        assert wait_job(client, service, queued.json()["data"]["job"]["id"])["state"] == "succeeded"
        document = operation("context_get", {"document_id": note["id"]})["document"]
        assert document["metadata"]["summary"] == "Ada needs a signed pilot."
        workspace = service.fixture.hq / "agents/business-agent/sessions"
        workspace.mkdir(exist_ok=True)
        session = new_id()
        (workspace / "selected.jsonl").write_text("\n".join(json.dumps(row) for row in [
            {"type": "user", "sessionId": session, "uuid": new_id(), "timestamp": "2026-09-08T00:00:00Z", "message": {"content": "Keep exact source evidence."}},
            {"type": "assistant", "sessionId": session, "uuid": new_id(), "timestamp": "2026-09-08T00:00:01Z", "message": {"content": [{"type": "text", "text": "The next step is the pilot."}]}}
        ]))
        (workspace / "old-harness.jsonl").write_text(json.dumps({"ts": "2026-01-01", "kind": "user", "model": "old", "agent": "business-agent", "content": "Old harness record"}))
        snapshots = LocalFiles(service.runtime).transcripts(service.fixture.business_id, service.agent.id)
        assert [snapshot["session_id"] for snapshot in snapshots] == ["selected"]
        assert len(model.calls) == 1
        removed = client.post("/api/biz/widget/compact", json={"agent_id": service.agent.id}, headers=ORIGIN)
        assert removed.status_code == 404


def test_transcript_recognition_requires_claude_message_and_session_identity():
    assert not _is_claude_transcript(b'{}\nnot-json\n{"kind":"assistant","content":"legacy"}')
    assert not _is_claude_transcript(b'{"type":"assistant","message":{"content":"not registered Claude JSONL"}}')
    assert not _is_claude_transcript(b'{"type":"assistant","sessionId":"session","message":"wrong shape"}')
    assert _is_claude_transcript(b'{"type":"user","sessionId":"session","message":{"content":"Selected session"}}')


def routine_failure_fixture(fixture, now):
    within = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    outside = (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    with fixture.db.transaction() as uow:
        uow.repository("routines", business_id=fixture.business_id).insert({
            "namespace": fixture.business_id, "id": "filer", "name": "Filer", "scope": "business",
            "target": "business", "schedule": "*/5 * * * *", "timezone": "UTC", "status": "active",
            "job_kind": "context.file", "arguments_json": {}, "timeout_seconds": 600})
        uow.repository("routines", business_id=None).insert({
            "namespace": "global", "id": "sync", "name": "Sync", "scope": "global", "target": "each-business",
            "schedule": "0 * * * *", "timezone": "UTC", "status": "active", "job_kind": "test",
            "arguments_json": {}, "timeout_seconds": 60})
        for identity, namespace, business, state, started in [
            ("recent-failed", fixture.business_id, fixture.business_id, "failed", within),
            ("recent-succeeded", fixture.business_id, fixture.business_id, "succeeded", within),
            ("old-failed", fixture.business_id, fixture.business_id, "failed", outside),
            ("global-failed", "global", None, "failed", within),
        ]:
            uow.repository("routine_runs", business_id=business).insert({
                "id": identity, "namespace": namespace, "routine_id": "filer" if namespace != "global" else "sync",
                "trigger": "schedule", "state": state, "started_at": started, "finished_at": started,
                "result_json": {}, "cost_json": {}})


def test_live_routine_failures_are_windowed_by_state_recency_and_scope(tmp_path):
    """live_routine_failures() (PR2) queries routine_runs directly instead of
    routine_history()'s exhaustive scan. The dashboard's browser session is
    always the installation owner (human_global_capabilities grants the full
    GLOBAL_CAPABILITIES set to that one principal), so /live's real, reachable
    behavior is "owner sees business and global failures both" -- exercised
    here over HTTP. The complementary "caller lacks manage_global_routines"
    branch has no reachable HTTP path today (only the owner's browser session
    calls /live) and is proven directly below instead.
    """
    service = ServiceFixture(tmp_path, full=True)
    fixture = service.fixture
    now = datetime.now(timezone.utc)
    routine_failure_fixture(fixture, now)
    with TestClient(service.web) as client:
        service.login(client)
        live = client.get("/api/biz/widget/live")
        assert live.status_code == 200, live.text
        ids = [row["id"] for row in live.json()["routine_failures"]]
        # The global row is grouped by fanout identity, not its raw run id
        # (matching routines/reads.py's activity() and js/routines.js's
        # rtFailureToasts, which key on the same "global-"+hash shape), so
        # only the business-owned id is known verbatim ahead of time.
        assert len(ids) == 2
        assert "recent-failed" in ids
        assert sum(i.startswith("global-") for i in ids) == 1


def test_live_routine_failures_exclude_global_without_capability(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    fixture = service.fixture
    now = datetime.now(timezone.utc)
    routine_failure_fixture(fixture, now)
    compatibility = DashboardCompatibility(service.runtime, background=service.runtime.background)
    scoped = InvocationContext(Principal(fixture.owner.id, "human"), business_id=fixture.business_id,
        allowed_business_ids=frozenset({fixture.business_id}), source="dashboard", global_capabilities=frozenset())
    assert [row["id"] for row in compatibility.live_routine_failures(scoped, now)] == ["recent-failed"]


def test_notes_list_drops_body_and_shares_one_read_unit_for_filing_lookups(tmp_path, monkeypatch):
    """PR3: document_get's "notes" branch used to open its own
    runtime.database.read() inside page_view() for every upload-linked note.
    Proven here as a delta, not an absolute count, so the assertion doesn't
    depend on how many read units notes_list's own dispatch happens to use:
    linking two notes to an upload must not cost more Database.read() calls
    than linking none, because both notes now share the single unit the
    "notes" route opens around the whole list comprehension.
    """
    service = ServiceFixture(tmp_path, full=True)
    fixture = service.fixture

    def make_upload():
        data = document_bytes({"title": "Legacy import"}, "Saved interview.")
        path = "context/_dumps/" + new_id() + ".md"
        (fixture.hq / path).parent.mkdir(parents=True, exist_ok=True)
        (fixture.hq / path).write_bytes(data)
        with fixture.db.transaction() as uow:
            return uow.repository("documents", business_id=fixture.business_id).insert({
                "id": new_id(), "kind": "context_dump", "namespace": fixture.business_id, "path": path,
                "metadata_json": {"title": "Legacy import"}, "content_hash": hashlib.sha256(data).hexdigest()})

    def link(note_ids, upload_id):
        with fixture.db.transaction() as uow:
            documents = uow.repository("documents", business_id=fixture.business_id)
            for note_id in note_ids:
                row = documents.get(note_id, required=True)
                metadata = {k: v for k, v in row["metadata_json"].items() if k != "upload_id"}
                if upload_id:
                    metadata["upload_id"] = upload_id
                documents.update(note_id, {"metadata_json": metadata}, expected_revision=row["revision"])

    with TestClient(service.web) as client:
        service.login(client)
        def create_note(title):
            result = client.post("/api/biz/widget/notes", json={"title": title, "content": "Body text " + title}, headers=ORIGIN)
            assert result.status_code == 200, result.text
            return result.json()

        first, second = create_note("First"), create_note("Second")
        upload = make_upload()

        counts = []
        original = Database.read
        def counting(self, **kwargs):
            counts.append(1)
            return original(self, **kwargs)
        monkeypatch.setattr(Database, "read", counting)

        link([first["id"], second["id"]], None)
        unlinked = client.get("/api/biz/widget/notes", headers=ORIGIN)
        assert unlinked.status_code == 200, unlinked.text
        reads_unlinked = len(counts)

        counts.clear()
        link([first["id"], second["id"]], upload["id"])
        linked = client.get("/api/biz/widget/notes", headers=ORIGIN)
        assert linked.status_code == 200, linked.text
        reads_linked = len(counts)

        assert reads_linked == reads_unlinked

        rows = {row["id"]: row for row in linked.json()}
        assert set(rows) == {first["id"], second["id"]}
        for identity, title in ((first["id"], "First"), (second["id"], "Second")):
            assert "body" not in rows[identity]
            assert rows[identity]["content"] == "Body text " + title
            assert rows[identity]["filing"]["state"] == "processed"


def test_context_and_routines_processes_views_bucket_jobs_without_the_tree_or_history(tmp_path, monkeypatch):
    """PR4: /context?view=processes and /routines?view=processes must match
    context_view()'s and BackgroundService.status()'s own bucketing exactly,
    without walking the context tree or routine_history(). filing and
    summarizing use different queued-state sets on purpose (filing only
    counts literal "queued"; summarizing also counts "waiting_signoff",
    matching context_view()'s jobs() helper) -- this proves both, not just
    one. status() (the routines view) is not routine-linked-jobs-only, so the
    same fixture's context.file/context.map jobs are exactly what it should
    report as the business's "current"/"queued" background work too.
    """
    service = ServiceFixture(tmp_path, full=True)
    fixture, background = service.fixture, service.runtime.background
    monkeypatch.setattr(background, "start", lambda: None)  # keep the synthetic job rows exactly as inserted
    started = "2026-09-01T00:00:00Z"

    def job(kind, state):
        with fixture.db.transaction() as uow:
            return uow.repository("jobs", business_id=fixture.business_id).insert({
                "id": new_id(), "target_namespace": fixture.business_id, "kind": kind,
                "principal_id": fixture.owner.id, "arguments_json": {}, "dedupe_key": new_id(),
                "state": state, "updated_at": started})

    running_file = job("context.file", "applying")
    job("context.file", "queued")
    job("context.file", "waiting_signoff")
    map_job = job("context.map", "running")
    job("context.summarize", "waiting_signoff")

    with TestClient(service.web) as client:
        service.login(client)
        processes = client.get("/api/biz/widget/context", params={"view": "processes"}, headers=ORIGIN)
        assert processes.status_code == 200, processes.text
        body = processes.json()
        assert set(body) == {"filing", "summarizing", "has_filer"}
        assert body["has_filer"] is True
        assert body["filing"]["current"]["id"] == running_file["id"]
        assert body["filing"]["queued"] == 1  # only the literal "queued" file job, not waiting_signoff
        assert body["summarizing"]["current"]["id"] == map_job["id"]
        assert body["summarizing"]["current"]["topic"] == "map:"
        assert body["summarizing"]["queued"] == 1  # the waiting_signoff summarize job counts here

        routines = client.get("/api/biz/widget/routines", params={"view": "processes"}, headers=ORIGIN)
        assert routines.status_code == 200, routines.text
        rbody = routines.json()
        assert set(rbody) == {"current", "queued", "invalid"}
        # status() is scoped by business_id alone, any job kind: the earliest
        # running/applying job for this business (by insertion order), not
        # anything routine-specific.
        assert rbody["current"]["id"] == running_file["id"]
        assert rbody["queued"] == 1  # only the one literal "queued" job; status() never counts waiting_signoff either

        root_view = client.get("/api/root/routines", params={"view": "processes"}, headers=ORIGIN)
        assert root_view.status_code == 200, root_view.text
        root_body = root_view.json()
        assert set(root_body) == {"current", "queued", "invalid"}
        # Root asks status() for business_id=None (global-scoped jobs only),
        # so this business's jobs must not leak into the root/global view.
        assert root_body["current"] is None
        assert root_body["queued"] == 0
