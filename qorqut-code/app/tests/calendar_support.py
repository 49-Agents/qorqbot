"""Calendar fixtures on the v1.2 host with an empty, owned temporary instance."""

from contextlib import contextmanager

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.bootstrap.seed import apply_initial_plan
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id
from host_foundation_support import build_instance, synthetic_ordubashy


CALENDAR_OPERATIONS = ("calendar_calendars_list", "calendar_events_list", "calendar_event_get", "calendar_event_create",
                       "calendar_event_update", "calendar_event_delete", "calendar_event_respond")


@contextmanager
def calendar_instance(tmp_path, *, extensions=None):
    instance, _ = build_instance(tmp_path, extensions=extensions)
    instance.runtime.freeze_registry()
    try:
        yield instance
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def create_calendar_agent(instance, name, role="ordubashy"):
    """Publish the selected source role and register a synthetic Global agent, without starting a session."""
    if role == "ordubashy":
        # The calendar boundary does not depend on the role package's tool policy beyond allowing the calendar tools.
        synthetic_ordubashy(instance, allow=CALENDAR_OPERATIONS)
    owner = InvocationContext(Principal(instance.owner, "human"), allowed_business_ids=frozenset({instance.business}),
                              global_capabilities=GLOBAL_CAPABILITIES,
                              source="dashboard")
    runtime = instance.runtime
    with instance.database.read() as uow:
        plan = runtime.publisher.prepare(owner, {"source_id": role},
            InvocationOptions(idempotency_key=new_id()), uow, "role")
    pin = apply_initial_plan(instance.database, owner, plan, runtime.publisher,
                            runtime.publisher.resolve_file_target, runtime.locks)["artifact"]
    created = instance.invoke(owner, "agents_create", {
        "role": pin, "plugins": [], "name": name, "display_name": name,
        "assignment_brief": "Synthetic calendar fixture.",
        "scope": {"kind": "ecosystem", "allowed_business_ids": [instance.business],
                  "global_capabilities": ["read_global_context", "manage_calendar"]}})
    assert created.status == "ok", created.error
    return Principal(created.data["agent_id"], "agent")
