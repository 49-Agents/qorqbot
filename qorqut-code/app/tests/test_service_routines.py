from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from test_service_transports import ORIGIN, ServiceFixture


def test_49ide_global_history_status_errors_and_stop_use_real_http_contract(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path, full=True)
    fixture, background = service.fixture, service.runtime.background
    monkeypatch.setattr(background, "start", lambda: None)
    business_ids = [fixture.business_id]
    with fixture.db.transaction() as uow:
        uow.repository("businesses").update(fixture.business_id, {"slug": "ProjectA", "name": "ProjectA"}, expected_revision=1)
        for slug in ("ProjectB", "ProjectC", "ProjectD", "private-business"):
            identity = new_id()
            hq = fixture.root / slug / "hq"
            hq.mkdir(parents=True)
            uow.repository("businesses").insert({"id": identity, "slug": slug, "name": slug,
                "workspace": str(hq.parent), "hq_path": str(hq), "stage": "validating"})
            if slug != "private-business":
                uow.repository("business_memberships", business_id=identity).insert({"principal_id": fixture.owner.id, "is_owner": True})
                business_ids.append(identity)
            else:
                hidden_business = identity
        background.authority.register_workers(uow, business_ids)
    with TestClient(service.web) as client:
        service.login(client)
        def mutate(payload):
            response = client.post("/api/root/routines", json=payload, headers=ORIGIN)
            assert response.status_code == 200, response.text
            return response.json()["data"]
        routine = mutate({"op": "add", "definition": {"name": "ProjectA example across businesses", "program": "repository.status",
            "arguments": {}, "schedule": "0 0 * * *", "timezone": "America/New_York", "status": "paused", "target": "each-business"}})["records"][0]
        first = mutate({"op": "run", "id": routine["id"]})["job_ids"]
        assert len(first) == 4
        with fixture.db.transaction() as uow:
            for index, identity in enumerate(first):
                business = uow.connection.execute("SELECT business_id FROM jobs WHERE id=?", (identity,)).fetchone()[0]
                jobs = uow.repository("jobs", business_id=business)
                job = jobs.get(identity)
                runs = uow.repository("routine_runs", business_id=business)
                run = runs.get(job["definition_ref_json"]["routine_run_id"])
                jobs.update(identity, {"state": "succeeded"}, expected_revision=job["revision"])
                runs.update(run["id"], {"state": "succeeded", "finished_at": run["started_at"],
                    "result_json": {**run["result_json"], "data": {"stdout": " M README.md", "returncode": 0}},
                    "cost_json": {"usd": .1 * (index + 1), "calls": [{"model": "test-model", "usd": .1 * (index + 1)}]}}, expected_revision=run["revision"])
            uow.repository("routine_runs", business_id=hidden_business).insert({"id": "private-run", "namespace": "global", "routine_id": routine["id"],
                "trigger": "manual", "state": "succeeded", "started_at": run["started_at"], "finished_at": run["started_at"],
                "result_json": {"run_group_id": run["result_json"]["run_group_id"], "data": {"stdout": "PRIVATE OUTPUT"}}, "cost_json": {"usd": 999}})
        history = client.get("/api/biz/ProjectA/routines/runs?n=1")
        assert history.status_code == 200, history.text
        summary = history.json()[0]
        assert summary["usd"] == 1
        assert len(summary["runs"]) == 4
        assert {row["slug"] for row in summary["runs"]} == {"ProjectA", "ProjectB", "ProjectC", "ProjectD"}
        assert summary["name"] == routine["name"] and summary["zone"] == "America/New_York"
        assert summary["runs"][0]["stdout_tail"] == " M README.md"
        assert "PRIVATE" not in history.text and "private-business" not in history.text
        second = mutate({"op": "run", "id": routine["id"]})["job_ids"]
        assert len(client.get("/api/biz/ProjectA/routines/runs").json()) == 2
        with fixture.db.transaction() as uow:
            identity = second[0]
            business = uow.connection.execute("SELECT business_id FROM jobs WHERE id=?", (identity,)).fetchone()[0]
            job = uow.repository("jobs", business_id=business).get(identity)
            uow.repository("jobs", business_id=business).update(identity, {"state": "running"}, expected_revision=job["revision"])
        background.last_error = "forbidden"
        from datetime import datetime
        from bazarlyk.transport.routine_views import hourly_runs
        monkeypatch.setattr("bazarlyk.transport.compatibility.hourly_runs", lambda runs: hourly_runs(runs,
            now=datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))))
        response = client.get("/api/biz/ProjectA/routines")
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["error"] is None
        hours = view["hourly"]["routines"]["global/" + routine["id"]]
        assert len(hours) == 48
        assert sum(hour["succeeded"] for hour in hours) == 1
        assert sum(hour["failed"] for hour in hours) == 0
        assert view["current"]["key"] == "global/" + routine["id"]
        assert view["current"]["definition_ref_json"]["scope"] == "global"
        assert "skip_if_live" not in view["routines"][0]
        mutate({"op": "stop", "id": routine["id"], "job_id": identity, "target_business_id": business})
        with fixture.db.read() as uow:
            assert uow.repository("jobs", business_id=business).get(identity)["state"] == "cancel_requested"
