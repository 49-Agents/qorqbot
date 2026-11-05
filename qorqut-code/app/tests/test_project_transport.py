"""The real transport surfaces of the Project operations: dashboard HTTP routes and MCP argument splitting."""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import InvocationOptions, new_id, plain
from starlette.testclient import TestClient

from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.compatibility import DashboardCompatibility
from bazarlyk.transport.mcp import split_options
from test_project_records import (FORM, agent, call, context, context_global, host, ok, reminder, seed_project, task)


def web(host, tmp_path):
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    return TestClient(create_app(host.runtime, dashboard_root=dashboard, origins=["http://testserver"],
                                 authentication=Authentication(host.db, host.runtime.paths),
                                 compatibility=DashboardCompatibility(host.runtime), manage_lifecycle=False),
                      base_url="http://testserver")


def post(client, path, body):
    return client.post(path, headers={"Origin": "http://testserver"}, json=body)


def test_every_project_operation_runs_through_the_real_root_dashboard_route(host, tmp_path):
    project = seed_project(host, name="Transport Studio", slug="transport-studio")
    foreign = seed_project(host, name="Transport Source", slug="transport-source")
    row = task(host, foreign, "A transport-linked record")
    with web(host, tmp_path) as client:
        record = post(client, "/api/root/operations/project_record_get",
                      {"scope": "global", "project_id": project})
        assert record.status_code == 200, record.text
        assert record.json()["data"]["project"]["id"] == project
        listing = post(client, "/api/root/operations/project_items_list",
                       {"scope": "global", "project_id": project, "limit": 25})
        assert listing.status_code == 200, listing.text
        candidates = post(client, "/api/root/operations/project_items_candidates",
                          {"scope": "global", "project_id": project, "source_business_id": foreign,
                           "type": "task", "limit": 25})
        assert candidates.status_code == 200, candidates.text
        assert row["id"] in {item["id"] for item in candidates.json()["data"]["items"]}
        item = post(client, "/api/root/operations/project_item_get",
                    {"scope": "global", "project_id": project,
                     "item": {"type": "task", "id": row["id"], "business_id": foreign}})
        assert item.status_code == 200, item.text
        assert item.json()["data"]["record"]["task"]["id"] == row["id"]
        linked = post(client, "/api/root/operations/project_items_link",
                      {"scope": "global", "project_id": project, "idempotency_key": new_id(),
                       "item": {"type": "task", "id": row["id"], "business_id": foreign}})
        assert linked.status_code == 200, linked.text
        link = linked.json()["data"]["link"]
        removed = post(client, "/api/root/operations/project_items_unlink",
                       {"scope": "global", "project_id": project, "idempotency_key": new_id(),
                        "link_id": link["id"], "expected_revision": link["revision"]})
        assert removed.status_code == 200, removed.text
        assert removed.json()["data"] == {"removed": True}
        # The narrow reminder read is on the same owner root surface.
        global_reminder = reminder(host, None, "Owner transport reminder")
        reminders = post(client, "/api/root/operations/owner_reminders_list",
                         {"scope": "global", "limit": 25})
        assert reminders.status_code == 200, reminders.text
        assert global_reminder in {value["reminder_id"] for value in reminders.json()["data"]["reminders"]}
        wrong_scope = post(client, "/api/root/operations/owner_reminders_list", {"scope": "business", "limit": 25})
        assert wrong_scope.status_code in {400, 422}, wrong_scope.text
        assert wrong_scope.json()["error"]["code"] == "scope_required"


def test_a_business_route_must_agree_with_project_id_even_with_body_scope_global(host, tmp_path):
    project = seed_project(host, name="Route Target", slug="route-target")
    with web(host, tmp_path) as client:
        mismatched = post(client, "/api/biz/first/operations/project_record_get",
                          {"scope": "global", "project_id": project})
        assert mismatched.status_code == 403, mismatched.text
        assert mismatched.json()["error"]["code"] == "forbidden"
        matched = post(client, "/api/biz/route-target/operations/project_record_get",
                       {"scope": "global", "project_id": project})
        assert matched.status_code == 200, matched.text


def test_mcp_option_splitting_preserves_the_wire_arguments_for_all_six(host):
    project = seed_project(host, name="MCP Studio", slug="mcp-studio")
    foreign = seed_project(host, name="MCP Source", slug="mcp-source")
    row = task(host, foreign, "An MCP-linked record")
    wire = [
        ("project_record_get", {"scope": "global", "project_id": project}, None),
        ("project_items_list", {"scope": "global", "project_id": project, "limit": 25}, None),
        ("project_items_candidates", {"scope": "global", "project_id": project, "source_business_id": None}, None),
        ("project_item_get", {"scope": "global", "project_id": project,
                              "item": {"type": "task", "id": row["id"], "business_id": foreign}}, None),
    ]
    for name, arguments, _ in wire:
        args, options = split_options(dict(arguments))
        assert set(args) == set(arguments) - ({"business_id"} & set(args))
        result = host.runtime.application.invoke(context_global(host), name, args, options)
        assert result.status == "ok", (name, result.error)
    link_args, link_options = split_options({"scope": "global", "project_id": project, "idempotency_key": new_id(),
                                             "item": {"type": "task", "id": row["id"], "business_id": foreign}})
    assert "idempotency_key" not in link_args and link_options.idempotency_key
    linked = host.runtime.application.invoke(context_global(host), "project_items_link", link_args, link_options)
    assert linked.status == "ok", linked.error
    link = plain(linked.data)["link"]
    # split_options moves expected_revision into the invocation options exactly as HTTP and MCP always do.
    unlink_args, unlink_options = split_options({"scope": "global", "project_id": project, "idempotency_key": new_id(),
                                                 "link_id": link["id"], "expected_revision": link["revision"]})
    assert "expected_revision" not in unlink_args and unlink_options.expected_revision == 1
    removed = host.runtime.application.invoke(context_global(host), "project_items_unlink", unlink_args, unlink_options)
    assert removed.status == "ok", removed.error


def test_routine_runs_and_jobs_are_reachable_through_the_real_owner_paths(host, tmp_path):
    project = seed_project(host, name="Runner Court", slug="runner-court")
    now = "2026-10-20T00:00:00Z"
    project_routine, global_routine = new_id(), new_id()
    with host.db.transaction() as uow:
        for business, identity, name in ((project, project_routine, "Project recap"),
                                          (None, global_routine, "Global recap")):
            scope = "business" if business else "global"
            uow.connection.execute(
                "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,"
                "arguments_json,timeout_seconds,metadata_json,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?, '0 0 * * *','UTC','paused','test.fixture','{}',900,'{}',1,?,?)",
                (business or "global", business, identity, name, scope, scope, now, now))
        for identity, routine, business in ((new_id(), project_routine, project), (new_id(), global_routine, None)):
            uow.connection.execute(
                "INSERT INTO routine_runs(id,namespace,routine_id,business_id,trigger,slot,state,started_at,result_json,"
                "cost_json,revision,created_at,updated_at) VALUES(?,?,?,?,'manual',NULL,'succeeded',?,'{}','{}',1,?,?)",
                (identity, business or "global", routine, business, now, now, now))
        project_job, global_job = new_id(), new_id()
        for identity, business, namespace in ((project_job, project, "business:" + project),
                                               (global_job, None, "global")):
            uow.connection.execute(
                "INSERT INTO jobs(id,business_id,target_namespace,kind,principal_id,initiated_by_principal_id,"
                "arguments_json,definition_ref_json,dedupe_key,state,attempts,revision,created_at,updated_at) "
                "VALUES(?,?,?,'test.fixture',?,?,'{}','{}',?,'queued',0,1,?,?)",
                (identity, business, namespace, host.instance.owner, host.instance.owner, "dedupe-" + identity, now, now))
    with web(host, tmp_path) as client:
        project_runs = post(client, "/api/biz/runner-court/operations/routines_runs",
                            {"routine_id": project_routine, "limit": 50})
        assert project_runs.status_code == 200, project_runs.text
        assert project_runs.json()["data"]["items"][0]["routine_id"] == project_routine
        global_runs = post(client, "/api/root/operations/routines_runs",
                           {"scope": "global", "routine_id": global_routine, "limit": 50})
        assert global_runs.status_code == 200, global_runs.text
        assert global_runs.json()["data"]["items"][0]["routine_id"] == global_routine
        project_job_result = post(client, "/api/biz/runner-court/operations/jobs_get", {"job_id": project_job})
        assert project_job_result.status_code == 200, project_job_result.text
        assert project_job_result.json()["data"]["job"]["id"] == project_job
        assert project_job_result.json()["data"]["details_available"] is True
        global_job_result = post(client, "/api/root/operations/jobs_get", {"scope": "global", "job_id": global_job})
        assert global_job_result.status_code == 200, global_job_result.text
        assert global_job_result.json()["data"]["job"]["id"] == global_job
        # The exact source scope is not interchangeable, and root reads are Global only.
        wrong_scope = post(client, "/api/root/operations/jobs_get", {"scope": "global", "job_id": project_job})
        assert wrong_scope.status_code == 404, wrong_scope.text
        root_project_scope = post(client, "/api/root/operations/jobs_get", {"job_id": project_job})
        assert root_project_scope.status_code == 403, root_project_scope.text
        business_global = post(client, "/api/biz/runner-court/operations/jobs_get",
                               {"scope": "global", "job_id": global_job})
        assert business_global.status_code == 403, business_global.text
        business_global_runs = post(client, "/api/biz/runner-court/operations/routines_runs",
                                    {"scope": "global", "routine_id": global_routine})
        assert business_global_runs.status_code == 403, business_global_runs.text
        root_without_scope = post(client, "/api/root/operations/routines_runs", {"routine_id": project_routine})
        assert root_without_scope.status_code == 403, root_without_scope.text
        project_reading_global_routine = post(client, "/api/biz/runner-court/operations/routines_runs",
                                             {"routine_id": global_routine})
        assert project_reading_global_routine.status_code == 200, project_reading_global_routine.text
        assert project_reading_global_routine.json()["data"]["items"] == []
