"""Public Task vocabulary over the retained storage and receipt representation."""

from dataclasses import replace

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.contracts import plain

REQUEST_TO_OWNER = "request_to_owner"
STORED_OWNER_REQUEST = "owner_request"
KIND_INPUT = {
    "enum": ["ordinary", REQUEST_TO_OWNER, STORED_OWNER_REQUEST],
    "description": instruction_text("tasks-kind-normal-work-or-request-to-owner"),
}


def normalize_arguments(arguments):
    """Keep old and new spelling identical before request hashes and plans are built."""
    arguments = plain(arguments)
    if arguments.get("kind") == REQUEST_TO_OWNER:
        arguments["kind"] = STORED_OWNER_REQUEST
    changes = arguments.get("changes")
    if isinstance(changes, dict) and changes.get("kind") == REQUEST_TO_OWNER:
        changes["kind"] = STORED_OWNER_REQUEST
    return arguments


def public_task(task):
    """Copy one typed Task DTO without interpreting any authored nested fields."""
    return {**task, "kind": REQUEST_TO_OWNER} if task.get("kind") == STORED_OWNER_REQUEST else task


def project_result(result):
    """Present typed Task rows after receipt replay; leave history and authored fields intact."""
    if result.status != "ok" or result.data is None:
        return result
    data = plain(result.data)
    if not isinstance(data, dict):
        return result
    if isinstance(data.get("task"), dict):
        data["task"] = public_task(data["task"])
    if isinstance(data.get("tasks"), list):
        data["tasks"] = [public_task(task) if isinstance(task, dict) else task for task in data["tasks"]]
    return replace(result, data=data)
