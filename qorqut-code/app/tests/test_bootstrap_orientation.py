"""Global agents' bootstrap orientation: the business directory (the owner has no living brief)."""
import json

from bazarlyk.agents.context import business_aliases
from qorqut_operations.contracts import new_id
from test_agent_configuration import Fixture


def test_business_aliases_merge_explicit_names_slug_and_domains():
    row = {"display_name": "X Reply", "slug": "x-reply", "config_json": json.dumps({
        "aliases": ["Gadgetly", "gadgetly", " X  Replier "], "domain": "gadgetly.example",
        "website": "https://www.gadgetly.example/app", "email": "owner@gadgetly.example"})}
    assert business_aliases(row) == ["Gadgetly", "X Replier", "gadgetly.example", "www.gadgetly.example"]
    assert business_aliases({"display_name": "AI Automation Agency", "slug": "ai-automation-agency", "config_json": {}}) == []
    assert business_aliases({"display_name": "Acme", "slug": "acme-contractor",
                             "config_json": {"aliases": "not a list", "email": "owner@acme.example"}}) == [
        "acme-contractor", "acme.example"]
    many = [f"Name {i}" for i in range(20)]
    assert business_aliases({"display_name": "Copper", "slug": "copper-studio",
                             "config_json": {"aliases": many, "domain": "copper.dev"}}) == [*many, "copper-studio", "copper.dev"]


def test_global_agents_get_the_business_directory_and_no_owner_brief(tmp_path):
    fixture = Fixture(tmp_path)
    with fixture.db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(),
                                               "owner_principal_id": fixture.owner.id})
        uow.connection.execute("UPDATE principals SET display_name='Sam Owner' WHERE id=?", (fixture.owner.id,))
        uow.connection.execute("UPDATE businesses SET config_json=? WHERE id=?",
                               (json.dumps({"one_liner": "Widgets for testers.", "aliases": ["Gizmo"],
                                            "website": "https://widget.example"}), fixture.business_id))
    founder = fixture.create("founder", "founder")
    rendered = fixture.resolver.resolve_agent_config(founder).to_dict()["mandatory_context"]["rendered"]
    assert "Owner: Sam Owner." in rendered and "listed under Projects" in rendered
    assert "About The Owner" not in rendered and "Latest:" not in rendered
    assert "Treat every alias as the same project." in rendered
    assert (f"- Widget ({fixture.business_id}); stage: validating. Also called: Gizmo, widget.example. Widgets for testers."
            ) in rendered
    agent = fixture.create()
    business = fixture.resolver.resolve_agent_config(agent).to_dict()["mandatory_context"]["rendered"]
    assert "About The Owner" not in business and "## Businesses" not in business
    assert f"Business: Widget; UUID: {fixture.business_id}; stage: validating;" in business and "Also called: Gizmo, widget.example." in business
