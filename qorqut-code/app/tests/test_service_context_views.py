import hashlib

from starlette.testclient import TestClient

from qorqut_operations.contracts import InvocationOptions, new_id
from qorqut_operations.documents.serialization import document_bytes
from bazarlyk.transport.compatibility import DashboardCompatibility
from test_service_transports import ServiceFixture


def test_imported_markdown_maps_people_and_source_links_render_through_real_http(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    fixture = service.fixture

    def document(kind, path, metadata, body):
        data = document_bytes(metadata, body)
        target = fixture.hq / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        with fixture.db.transaction() as uow:
            return uow.repository("documents", business_id=fixture.business_id).insert({
                "id": new_id(), "kind": kind, "namespace": fixture.business_id, "path": path,
                "metadata_json": metadata, "content_hash": hashlib.sha256(data).hexdigest()})

    upload = document("context_dump", "context/_dumps/legacy.md", {"title": "Pilot interview"}, "Saved interview.")
    title = "The complete imported title must survive without a new model word limit"
    mapping = document("context_map", "context/_maps/legacy.md", {
        "v": 1, "tree": "context", "title": title, "nodes": 4, "from": "legacy",
        "upload_id": upload["id"], "at": "2026-01-01T12:00:00Z"},
        "# " + title + "\n\n- Pilot\n  - Exact evidence\n- Next decision\n")
    ada = document("context_person", "context/_people/ada.md", {"name": "Ada", "title": "Ada", "summary": "Biography summary.",
        "legacy_source": {"path": "business/hq/context/_people/ada.md"}},
        "# Ada\n\nBiography.\n\n## Summary\n\nBiography summary.\n\n## Notes\n\n### 2026-01-01\nRecorded evidence.\n")
    bea = document("context_person", "context/_people/bea.md", {"name": "Bea", "title": "Bea", "summary": "New summary."},
        "Fresh note content.\n\n## Summary\n\nNew summary.\n")
    research_doc = document("context_topic", "context/research/_topic.md", {"title": "Research", "tree": "context"}, "Research.")
    launch = document("context_topic", "context/launch/_topic.md", {"title": "Launch", "tree": "context"}, "Launch.")
    note = document("context_note", "context/research/observation.md", {
        "title": "Observation", "people": ["ada", ada["id"], bea["id"]], "from": "legacy",
        "also": ["launch", "deleted-topic"], "also_topic_ids": [launch["id"], new_id()]}, "Exact evidence.")
    for kind, target, role in (("person", ada, "mentions"), ("person", bea, "mentions"),
            ("topic", research_doc, "part_of"), ("topic", launch, "concerns")):
        result = service.runtime.application.invoke(fixture.ctx, "relationships_create", {
            "source": {"type": "material", "id": note["id"]}, "target": {"type": kind, "id": target["id"]},
            "role": role}, InvocationOptions(idempotency_key=new_id()))
        assert result.status == "ok", result.to_dict()
    with TestClient(service.web) as client:
        service.login(client)
        result = client.get("/api/biz/widget/context")
        assert result.status_code == 200, result.text
        view = result.json()
        assert view["maps"][0]["document_id"] == mapping["id"]
        assert view["maps"][0]["tree"]["text"] == title
        assert view["maps"][0]["nodes"] == 4
        assert view["maps"][0]["tree"]["children"][0]["children"][0]["text"] == "Exact evidence"
        people = {item["slug"]: item for item in view["people"]}
        assert people["ada"]["mentions"] == ["research/observation.md"]
        assert people["bea"]["mentions"] == ["research/observation.md"]
        research = next(item for item in view["tree"]["children"] if item["path"] == "research")
        assert set(research["people"][note["id"]]) == {"ada", "bea"}
        assert research["also"][note["id"]] == ["launch"]
        assert client.get("/api/biz/widget/context/person/ada").json()["notes_text"] == "### 2026-01-01\nRecorded evidence."
        assert client.get("/api/biz/widget/context/person/bea").json()["notes_text"] == "Fresh note content."
        result = client.get("/api/biz/widget/context/map", params={"id": "legacy"})
        assert result.status_code == 200, result.text
        trace = result.json()["trace"]
        assert trace["document_id"] == upload["id"]
        assert trace["notes"][0]["document_id"] == note["id"]
        assert trace["topics"] == ["research"]
        assert {item["slug"] for item in trace["people"]} == {"ada", "bea"}
        assert result.json()["map"]["at"] == "2026-01-01T12:00:00Z"


def test_canonical_context_relationships_are_resolved_for_the_graph(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path, full=True)

    def create(operation, arguments):
        result = service.runtime.application.invoke(service.fixture.ctx, operation, arguments,
            InvocationOptions(idempotency_key=new_id()))
        assert result.status == "ok", result.to_dict()
        return result.data["document"]

    research = create("context_topics_create", {"title": "Research", "slug": "research"})
    launch = create("context_topics_create", {"title": "Launch", "slug": "launch"})
    person = create("context_people_create", {"name": "Ada", "role": "Pilot sponsor"})
    note = create("context_notes_create", {"title": "Pilot evidence", "body": "Ada confirmed the next pilot.",
        "topic_id": research["id"], "people_ids": [person["id"]], "also_topic_ids": [launch["id"]]})
    with TestClient(service.web) as client:
        service.login(client)
        response = client.get("/api/biz/widget/context")
        assert response.status_code == 200, response.text
        view = response.json()
        topic = next(item for item in view["tree"]["children"] if item["path"] == "research")
        name = note["id"]
        assert topic["also"][name] == ["launch"]
        assert topic["people"][name] == ["ada"]
        assert view["people"][0]["mentions"] == [note["path"].removeprefix("context/")]
        graph = view["graph"]
        assert any(edge["source"] == {"type": "material", "id": note["id"]}
            and edge["target"] == {"type": "topic", "id": research["id"]}
            and edge["role"] == "part_of" for edge in graph["edges"])
        def no_full_tree(*args):
            raise AssertionError("Graph must not load the document collection")
        monkeypatch.setattr(DashboardCompatibility, "context_view", no_full_tree)
        projection = client.get("/api/biz/widget/context?view=graph")
        assert projection.status_code == 200, projection.text
        # PR3 (STAGE2) adds an `etag;desc=hit|miss` entry alongside the
        # `;dur=` ones; this test only cares about the timing durations.
        timing = dict(part.strip().split(";dur=") for part in projection.headers["server-timing"].split(",")
            if ";dur=" in part)
        assert float(timing["total"]) >= 0
        assert float(timing["db"]) >= 0
        assert int(timing["units"]) >= 0
        assert int(timing["body"]) > 0
        assert projection.json() == {"graph_only": True, "graph": graph}


def test_structured_maps_keep_canonical_outline_and_person_sections_ignore_fenced_headings():
    outline = [{"text": "Published tree", "children": [{"text": "Canonical branch"}]}]
    view = DashboardCompatibility.map_view({"id": "map", "metadata": {"outline": outline},
        "body": "# Ignored legacy body"}, {"path": "context/_dumps/upload.md"})
    assert view["tree"] == outline[0]
    assert view["nodes"] == 2
    document = {"metadata": {"legacy_source": {}}, "body": "# Person\n\n```md\n## Notes\nNot a section\n```\n\n## Notes\nActual evidence.\n\n## Other\nOutside notes."}
    assert DashboardCompatibility.person_notes(document) == "Actual evidence."
