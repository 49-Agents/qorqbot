"""Document workflow adapters and compatibility projections for shared edges."""

from qorqut_operations.records.vocabulary import document_type
from qorqut_operations.relationships import create_edge, related_rows

LEGACY_FIELDS = {"parent_id", "topic_id", "also", "also_topic_ids", "people"}


def projection(row, uow):
    metadata = {key: value for key, value in row["metadata_json"].items() if key not in LEGACY_FIELDS}
    edges = related_rows(uow, row["business_id"], source={"type": document_type(row), "id": row["id"]})
    parents = list(dict.fromkeys(edge["to_id"] for edge in edges if edge["to_type"] == "topic" and edge["role"] == "part_of"))
    topics = list(dict.fromkeys([*parents, *[edge["to_id"] for edge in edges if edge["to_type"] == "topic" and edge["role"] in {"concerns", "mentions"}]]))
    metadata.update(topic_ids=topics, parent_ids=parents if row["kind"] == "context_topic" else [],
        topic_id=parents[0] if parents else None, also_topic_ids=[identity for identity in topics if identity not in parents],
        people=list(dict.fromkeys(edge["to_id"] for edge in edges if edge["to_type"] == "person" and edge["role"] == "mentions")))
    return metadata


def replace_targets(builder, uow, source, role, target_type, identities, *, operation_name=None):
    wanted = set(identities)
    existing = [edge for edge in related_rows(uow, builder.ctx.business_id, source=source, role=role) if edge["to_type"] == target_type]
    for edge in existing:
        if edge["to_id"] not in wanted:
            builder.record("relationships", edge["id"], edge, {}, action="delete", operation_name=operation_name)
    for identity in sorted(wanted - {edge["to_id"] for edge in existing}):
        create_edge(builder, source, {"type": target_type, "id": identity}, role, operation_name=operation_name)
