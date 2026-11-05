"""The real combined dashboard against the real composed owner HTTP application.

The synthetic host is composed by ``project_composition_support`` from this version's own host support (assembled
schema/catalog, owned admission, ``create_app``, dashboard compatibility, owner origin checks) and serves the combined
dashboard's static files. The focused browser driver in the dashboard repository talks only to that server: it does
not intercept, fabricate or stub a Project, Task, Focus, private-conversation, document/table, routine/job, Dev or
Signoff response. No live database, model, native agent, provider delivery, routine/job execution or service command
runs here.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from qorqut_operations.storage.inventory import compile_target

from project_composition_support import BROWSER_TEST, build_composed, call, ok, write_fixture

PLAYWRIGHT = "/tmp/node_modules/playwright"
# Current reviewed Move to project / CRM composition with the legacy learning objects removed.
# The separately accepted Overseer storage contribution remains additive to that exact lineage.
APPROVED_TARGET_DIGEST = "f15ae36d2193b8321c2a60d6b21dbd6e3e88bc9f7adee4af00b0d86cc064f263"
APPROVED_TARGET_OBJECTS = 522
APPROVED_TARGET_OBJECT_HASH = "728e743367facef89825bf54dddf99f281a5c48a6c3977d373348e9c4d801004"
OVERSEER_OWNER = "overseer"
COMPOSED_TARGET_DIGEST = "bf5c15191543d54b00f4c415b0b782c7b895decbb112cd913d8011e0e89d5a0d"
COMPOSED_TARGET_OBJECTS = 547
COMPOSED_TARGET_OBJECT_HASH = "4ec8a14a7cf62619c765ecef24946727e5b7ba90c4fed787ea0a6378575b29f5"


def object_hash(objects):
    payload = json.dumps(sorted([list(key) + [value[0], value[1]] for key, value in objects.items()]),
                         separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def project_lineage(assembly):
    """The same composition without the accepted Overseer integration's storage contribution.

    Both lines were integrated from separately reviewed sources: this Project qualification's approved snapshot and the
    Overseer storage owner. Excluding that one owner leaves exactly the approved Project-lineage composition, so the
    additive addition can be checked instead of merely overwriting the approved identity."""
    from qorqut_operations.storage.assembly import AssemblyBuilder

    from bazarlyk.composition.assembly import host_contributions
    builder = AssemblyBuilder(target_name=assembly.target.name, target_version=assembly.target.version)
    for contribution in host_contributions(assembly.change_publisher):
        if contribution.owner != OVERSEER_OWNER:
            builder.add(contribution)
    return builder.freeze()


def browser_available():
    return shutil.which("node") is not None and Path(PLAYWRIGHT).is_dir() and BROWSER_TEST.is_file()


@pytest.mark.skipif(not browser_available(), reason="node/playwright or the dashboard's composed driver is unavailable")
def test_the_real_dashboard_qualifies_the_composed_project_application(tmp_path):
    host = build_composed(tmp_path)
    try:
        composed = host.database.assembly
        objects = compile_target(composed).objects
        assert composed.target.digest == COMPOSED_TARGET_DIGEST, "the composed target differs from this integration's assembly"
        assert len(objects) == COMPOSED_TARGET_OBJECTS
        assert object_hash(objects) == COMPOSED_TARGET_OBJECT_HASH
        # The accepted Overseer integration is additive: the approved Project snapshot is byte-identical without its own
        # storage owner, so the addition changes no Project object and the approved identity above still holds.
        lineage_objects = compile_target(project_lineage(composed)).objects
        assert project_lineage(composed).target.digest == APPROVED_TARGET_DIGEST, \
            "the approved Project snapshot changed under the accepted addition"
        assert len(lineage_objects) == APPROVED_TARGET_OBJECTS
        assert object_hash(lineage_objects) == APPROVED_TARGET_OBJECT_HASH
        fixture = tmp_path / "fixture.json"
        write_fixture(host, fixture)
        shots = Path(os.environ.get("PROJECT_COMPOSITION_SHOTS", tmp_path / "shots"))
        shots.mkdir(parents=True, exist_ok=True)
        environment = {
            **os.environ,
            "PROJECT_COMPOSITION_BASE": host.origin,
            "PROJECT_COMPOSITION_FIXTURE": str(fixture),
            "PROJECT_COMPOSITION_SHOTS": str(shots),
            "PLAYWRIGHT_MODULE": os.environ.get("PLAYWRIGHT_MODULE", PLAYWRIGHT),
        }
        completed = subprocess.run(["node", str(BROWSER_TEST)], cwd=str(BROWSER_TEST.parent), env=environment,
                                   capture_output=True, text=True, timeout=900)
        print(completed.stdout)
        print(completed.stderr)
        assert completed.returncode == 0, "the composed browser qualification failed"
        _stored_invariants(host)
    finally:
        host.close()


def _rows(host, sql, *parameters):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def _stored_invariants(host):
    """Stored facts the browser run must leave exactly as the real contract says."""
    fixture = host.fixture
    harbor = fixture["projects"]["harbor"]
    tasks = _rows(host, "SELECT id,title,business_id FROM tasks")
    # Reading, pinning, linking, opening dashboards and sending private comments create no shadow Project Task and
    # change no Task title: the complete seeded title set is exact.
    assert sorted(row["title"] for row in tasks) == fixture["task_titles"], "the browser run created or renamed a Task"
    assert fixture["owned_linked"]["id"] in {row["id"] for row in tasks}
    # Linking and unlinking never changes ownership, parentage or the canonical scope of either side.
    foreign = _rows(host, "SELECT business_id, fields_json FROM tasks WHERE id=?", fixture["foreign"]["id"])[0]
    assert foreign["business_id"] == fixture["projects"]["maple"]
    assert "parent_id" not in json.loads(foreign["fields_json"]).get("$task", {})
    global_task = _rows(host, "SELECT business_id FROM tasks WHERE id=?", fixture["global"]["id"])[0]
    assert global_task["business_id"] is None
    assert {row["business_id"] for row in tasks if row["id"] == fixture["shared"]["id"]} == {
        harbor, fixture["projects"]["maple"]}, "the same Task id in two scopes stayed two distinct records"
    assert _rows(host, "SELECT business_id FROM tasks WHERE id=?", fixture["convertible"]["id"])[0]["business_id"] == harbor
    # Link and unlink round trips through the real envelopes leave exactly the seeded Project associations.
    links = sorted((row["target_type"], row["target_id"]) for row in
                   _rows(host, "SELECT target_type,target_id FROM project_item_links WHERE business_id=?", harbor))
    assert links == fixture["link_targets"], "the link/unlink round trips changed the Project's stored associations"
    # The private channel holds exactly the seed plus each explicit UI send, on the exact canonical source. A draft
    # never becomes a message, and no provider/comment reaches another record.
    counts = {row["source_key"]: row["messages"] for row in _rows(
        host, "SELECT c.source_key, COUNT(m.sequence) messages FROM owner_conversations c "
              "JOIN owner_conversation_messages m ON m.conversation_id=c.id GROUP BY c.source_key")}
    assert counts[f"source:{harbor}:project:{harbor}"] == 3
    assert counts[f"source:{harbor}:task:{fixture['story']['id']}"] == 1
    assert counts[f"source:{harbor}:signoff:{fixture['signoffs']['whatsapp']}"] == 1
    assert counts[f"source:global:task:{fixture['global_linked']['id']}"] == 1
    assert counts.get(f"source:global:signoff:{fixture['signoffs']['email']}", 0) == 0
    assert fixture["hidden"]["task_id"] in {row["id"] for row in tasks}
    # No provider operation ran while qualifying the retained Signoff records.
    assert _rows(host, "SELECT state FROM signoffs WHERE id=?", fixture["signoffs"]["whatsapp"])[0]["state"] == "pending"
    assert _rows(host, "SELECT state FROM signoffs WHERE id=?", fixture["signoffs"]["email"])[0]["state"] == "pending"
    assert not _rows(host, "SELECT 1 FROM signoff_notifications"), "no Signoff notification was produced"
    # The denied target is still stored, but the real inventory never emits its title after the membership change.
    owner = host.context(harbor)
    labels = []
    for section in ("work", "signoffs", "actions", "documents", "tables", "automation", "other"):
        labels += [item["label"] for item in ok(call(host, owner, "project_items_list", {
            "project_id": harbor, "section": section, "limit": 100,
            "include_closed": True, "include_archived": True}))["items"]]
    assert fixture["hidden"]["title"] not in labels, "the inventory leaked a denied target title"
    assert len(labels) == len(set(labels)), "the inventory repeated an identity"
    # The dashboard's save and conversion both used real revision-checked writes, so the fixture's records advanced.
    story = json.loads(_rows(host, "SELECT fields_json FROM tasks WHERE id=?", fixture["story"]["id"])[0]["fields_json"])
    assert story.get("$task", {}).get("overview") == "An edit that must not reorder Threads."
    converted = json.loads(_rows(host, "SELECT fields_json FROM tasks WHERE id=?", fixture["convertible"]["id"])[0]["fields_json"])
    assert converted.get("$task", {}).get("task_type") == "story"
