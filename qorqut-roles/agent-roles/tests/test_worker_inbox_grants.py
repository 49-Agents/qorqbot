"""Contract for the automatic controls needed to finish routed replies."""
from pathlib import Path
import yaml

def test_ordubashy_grants_entire_worker_lifecycle_without_domain_allowlist_entries():
    root=Path(__file__).resolve().parents[1]
    role=yaml.safe_load((root/'roles/ordubashy/role.yaml').read_text())
    controls={'delegated_assignment_get','delegated_assignment_checkpoint',
        'delegated_assignment_events_claim','delegated_assignment_event_settle','browser_turn'}
    assert all(role['default_tool_policy'].get(name)=='allow' for name in controls)
