from qorqut_operations.contracts import Principal
from test_agentic_installation import host


def test_real_configuration_uses_one_catalog_and_reads_next_revision(host, monkeypatch):
    import qorqut_agentic.catalog_api as api
    instance, manager, _ = host
    original = api.load_catalog
    loads = []

    def counted(source):
        loads.append(True)
        return original(source)

    monkeypatch.setattr(api, "load_catalog", counted)
    principal = Principal(manager, "agent")
    first = instance.runtime.resolver.resolve_agent_config(principal).to_dict()
    assert len(loads) == 1
    preferences = instance.runtime.agentic_layer.root / "user-preferences/SKILL.md"
    preferences.write_text(preferences.read_text() + "\nNew synthetic preference.\n")
    loads.clear()
    second = instance.runtime.resolver.resolve_agent_config(principal).to_dict()
    assert len(loads) == 1
    assert "New synthetic preference." in second["mandatory_context"]["rendered"]
    assert second["effective_config_id"] != first["effective_config_id"]
