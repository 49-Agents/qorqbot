"""Directional relationship values shared by the relationship operations and the reviews (the v1 generic helpers).

An edge row connects a source item to a target item with a role and an explanation. The helpers here carry no role
vocabulary: ``edge_view`` is given the host's role mapping to mark a role as predefined."""

from __future__ import annotations

from collections.abc import Mapping

from qorqut_operations.application.plans import rows
from qorqut_operations.contracts import new_id

ROLE = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"}
CONTEXT = {"type": "string", "maxLength": 20000}


def endpoints(edge):
    return ({"type": edge["from_type"], "id": edge["from_id"]}, {"type": edge["to_type"], "id": edge["to_id"]})


def edge_view(edge, roles: Mapping):
    source, target = endpoints(edge)
    return {**{key: value for key, value in edge.items() if key not in {"from_type", "from_id", "to_type", "to_id"}},
            "source": source, "target": target, "predefined": edge["role"] in roles}


def edge_values(source, target, role, context=""):
    return {"from_type": source["type"], "from_id": source["id"], "to_type": target["type"], "to_id": target["id"],
            "role": role, "context": context}


def related_rows(uow, business_id, *, source=None, target=None, role=None):
    clauses, parameters = [], []
    for prefix, item in (("from", source), ("to", target)):
        if item:
            clauses.extend((f"{prefix}_type=?", f"{prefix}_id=?"))
            parameters.extend((item["type"], item["id"]))
    if role is not None:
        clauses.append("role=?")
        parameters.append(role)
    return rows(uow, "relationships", business_id, where=" AND ".join(clauses), parameters=parameters)


def create_edge(builder, source, target, role, context="", *, identity=None, operation_name=None):
    """Record a new relationship in a prepared plan; for domain operations that create their own edges."""
    identity = identity or new_id()
    builder.record("relationships", identity, None, {"id": identity, **edge_values(source, target, role, context),
        "created_at": builder.at, "updated_at": builder.at, "created_by": builder.ctx.principal_id},
        operation_name=operation_name)
    return identity
