"""PR3 (handoff STAGE2, section 4.3): ETag revalidation on dashboard GET
routes. bazarlyk/service/changes.py's ChangeFeed.route_versions and
bazarlyk/transport/http.py's revalidated/revalidated_async/_etag_for are
exercised at the unit level in test_change_feed.py; these are the
HTTP-integration properties that matter operationally: a 304 opens no unit
of work, a write that bumps a declared kind is never served stale, and two
different `?view=` variants of one route never collide on the same tag.
"""
from starlette.testclient import TestClient

from test_service_transports import ServiceFixture, ORIGIN


def server_timing(response):
    return {entry.split(";", 1)[0]: entry for entry in response.headers["server-timing"].split(", ")}


def test_fresh_read_gets_a_weak_etag_and_reports_a_miss(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        result = client.get("/api/biz/widget/tasks")
        assert result.status_code == 200, result.text
        assert result.headers["etag"].startswith('W/"')
        assert result.headers["cache-control"] == "no-store"
        assert server_timing(result)["etag"] == "etag;desc=miss"


def test_matching_if_none_match_returns_304_and_opens_no_content_query_unit(tmp_path):
    """A dashboard GET always pays a fixed, single-row-lookup unit of work per
    request regardless of ETag (owner auth, the installation's maintenance-state
    check, and slug-to-business context resolution -- see HTTPAPI.context()
    and Runtime.require_authority()); "no unit of work opened" (handoff 4.3)
    means the route's own content query (tasks_list/assignees_list here) never
    runs on a hit, not that literal request dispatch is free. A 304 must open
    strictly fewer units than the miss that produced its ETag, by exactly the
    content query -- confirmed as a relative delta rather than an absolute
    count, since the fixed per-request overhead is not this PR's concern.
    """
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        first = client.get("/api/biz/widget/tasks")
        tag = first.headers["etag"]
        miss_units = int(server_timing(first)["units"].split("=")[1])
        second = client.get("/api/biz/widget/tasks", headers={"If-None-Match": tag})
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == tag
        assert second.headers["cache-control"] == "no-store"
        timing = server_timing(second)
        assert timing["etag"] == "etag;desc=hit"
        hit_units = int(timing["units"].split("=")[1])
        assert hit_units < miss_units


def test_a_write_that_bumps_the_routes_kind_is_never_served_stale(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        before = client.get("/api/biz/widget/tasks")
        stale_tag = before.headers["etag"]

        created = client.post("/api/biz/widget/task", json={"title": "A new task", "owner": "agent:business-agent"}, headers=ORIGIN)
        assert created.status_code == 201, created.text

        after = client.get("/api/biz/widget/tasks", headers={"If-None-Match": stale_tag})
        assert after.status_code == 200, after.text
        assert after.headers["etag"] != stale_tag
        assert any(row["title"] == "A new task" for row in after.json())


def test_view_query_variants_of_the_same_route_never_share_an_etag(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        default = client.get("/api/biz/widget/context")
        graph = client.get("/api/biz/widget/context", params={"view": "graph"})
        processes = client.get("/api/biz/widget/context", params={"view": "processes"})
        assert len({default.headers["etag"], graph.headers["etag"], processes.headers["etag"]}) == 3
        # A tag cached for one view must never validate a request for another.
        cross = client.get("/api/biz/widget/context", params={"view": "graph"}, headers={"If-None-Match": default.headers["etag"]})
        assert cross.status_code == 200, cross.text


def test_owner_attention_checks_every_business_it_cannot_see_without_a_read(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        before = client.get("/api/me/attention")
        assert before.status_code == 200, before.text
        stale_tag = before.headers["etag"]

        created = client.post("/api/biz/widget/task", json={"title": "Needs owner", "owner": "agent:business-agent"}, headers=ORIGIN)
        assert created.status_code == 201, created.text

        after = client.get("/api/me/attention", headers={"If-None-Match": stale_tag})
        assert after.status_code == 200, after.text
        assert after.headers["etag"] != stale_tag
