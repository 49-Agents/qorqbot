"""Marketing HTTP routes use the registered request domain, never the retired CLI bridge."""

from test_service_transports import ORIGIN


def test_retired_marketing_request_routes_are_not_registered(connected):
    _, client = connected
    for path in ("/api/biz/widget/requests", "/api/biz/widget/requests/retired"):
        assert client.get(path).status_code == 404
        assert client.post(path, json={}, headers=ORIGIN).status_code == 404


def test_legacy_chat_endpoint_is_explicitly_retired(connected):
    _, client = connected
    response = client.post("/api/biz/widget/chat", json={"message": "Do not launch a model"}, headers=ORIGIN)
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "legacy_chat_retired"
