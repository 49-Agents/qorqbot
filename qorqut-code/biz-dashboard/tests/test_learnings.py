"""Learning decisions use the real application; transcript extraction is covered in biz-core."""

from bazarlyk.contracts import new_id
from test_service_transports import ORIGIN


def test_learning_empty_counts_and_unknown_targets(connected):
    _, client = connected
    listed = client.get("/api/biz/widget/learnings")
    assert listed.status_code == 200, listed.text
    assert listed.json()["counts"]["open"] == 0
    assert listed.json()["items"] == []
    live = client.get("/api/biz/widget/live")
    assert live.status_code == 200, live.text
    assert live.json()["learnings_open"] == 0
    missing = client.post("/api/biz/widget/learnings/" + new_id() + "/state", json={"state": "accepted"}, headers=ORIGIN)
    assert missing.status_code == 404, missing.text
