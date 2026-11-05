"""Dashboard routines select registered programs; scheduler durability is tested in biz-core."""

from test_service_transports import ORIGIN


def test_typed_routine_definition_and_explicit_global_destination(connected):
    service, client = connected
    definition = {"name": "Registered check", "program": "repository.status", "arguments": {},
        "schedule": "0 9 * * *", "timezone": "UTC", "status": "paused"}
    response = client.post("/api/biz/widget/routines", json={"op": "add", "definition": definition}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    row = response.json()["data"]["records"][0]
    listing = client.get("/api/biz/widget/routines").json()
    assert any(item["id"] == row["id"] for item in listing["routines"])
    assert any(item["id"] == "repository.status" for item in listing["programs"])
    assert client.get("/api/root/routines").status_code == 200
    assert client.get("/api/root/routines", params={"review_business_id": service.fixture.business_id}).status_code == 422
    refused = client.post("/api/biz/widget/routines", json={"op": "set", "id": row["id"], "run": "arbitrary shell"}, headers=ORIGIN)
    assert refused.status_code == 422
    assert client.post("/api/routines/kill", json={}, headers=ORIGIN).status_code == 422
