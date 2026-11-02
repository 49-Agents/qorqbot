"""Trusted selected owner-input sources; absence never guesses provenance or grants access."""
from qorqut_operations.contracts import DomainError


def association(runtime, *args):
    found = []
    for name, adapter in runtime.selection.owner_input_sources.items():
        value = adapter.association(runtime, *args)
        if value is not None:
            found.append({**value, "input_type": name})
    return found[0] if len(found) == 1 else None


def adapter(runtime, linked):
    value = runtime.selection.owner_input_sources.get(linked.get("input_type"))
    if value is None:
        raise DomainError("evidence_unavailable", "The source extension is unavailable")
    return value


def references(runtime, source, linked):
    return adapter(runtime, linked).references(source, linked)


def record(runtime, uow, linked, linked_refs, **context):
    return adapter(runtime, linked).record(uow, linked, linked_refs, **context)
