"""The dashboard Task-create route forwards the three Customer fields unchanged and nothing else is newly accepted.

``DashboardCompatibility.business_post`` for ``POST .../task`` checks the request fields against a whitelist and forwards them to
``tasks_create``. Customer creation sends ``customer_kind``, ``customer_state`` and ``customer_profile`` with ``task_type``; the
route must pass them through verbatim (the engine alone validates and persists them), keep ``OPTIONS`` as invocation options and
keep refusing every other unknown field.

SYNTHETIC STUB. Only the route handler is under test: the engine call is a recording stub, so this runs against any engine and
proves nothing about Customer creation. The integration proof, through the real composed application and HTTP transport and the
completed Operations engine (``POST /api/biz/<slug>/task`` creating, then reading and updating persisted Customers), is
``test_host_customer_records.py``.
"""

from __future__ import annotations

import json

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.transport.compatibility import DashboardCompatibility

CUSTOMER = {"title": "Acme Ltd", "task_type": "customer", "customer_kind": "company", "customer_state": "Prospect",
            "customer_profile": {"company": "Acme Ltd", "contact_name": "A. Buyer", "email": "buyer@example.test",
                                 "phone": "+000", "website": "https://example.test", "notes": "Prefers morning calls."}}


class Recorder(DashboardCompatibility):
    """The real route handler with the engine call replaced by a recording stub."""

    def __init__(self):
        super().__init__(runtime=None, local_files=object())
        self.calls = []

    def invoke(self, context, operation, arguments=None, options=None):
        self.calls.append((operation, arguments, options))

        class Result:
            status = "ok"
            data = {"task": {"id": "task-1", "title": (arguments or {}).get("title")}}

        return Result()

    def task_view(self, context, task):
        return {"id": task["id"], "title": task["title"]}


def create(body):
    handler = Recorder()
    response = handler.business_post(None, object(), "business-1", ["task"], body)
    return handler, response


def test_customer_fields_reach_tasks_create_unchanged():
    handler, response = create(dict(CUSTOMER))
    assert response.status_code == 201 and json.loads(response.body) == {"task": {"id": "task-1", "title": "Acme Ltd"}}
    [(operation, arguments, options)] = handler.calls
    assert operation == "tasks_create"
    assert arguments == {**CUSTOMER, "assignee_ids": []}
    assert arguments["customer_profile"] == CUSTOMER["customer_profile"]            # nested notes untouched, not summarised or dropped
    assert options.idempotency_key


def test_invocation_options_stay_options_and_the_customer_fields_are_never_mistaken_for_them():
    handler, _ = create({**CUSTOMER, "idempotency_key": "key-1", "expected_revision": 3})
    [(_, arguments, options)] = handler.calls
    assert options.idempotency_key == "key-1" and options.expected_revision == 3
    assert "idempotency_key" not in arguments and "expected_revision" not in arguments
    assert {"customer_kind", "customer_state", "customer_profile", "task_type"} <= set(arguments)


def test_an_ordinary_create_is_forwarded_exactly_as_before():
    handler, _ = create({"title": "Plain", "tags": ["a"], "status": "open"})
    [(_, arguments, _)] = handler.calls
    assert arguments == {"title": "Plain", "tags": ["a"], "status": "open", "assignee_ids": []}


@pytest.mark.parametrize("body, field", [
    ({**CUSTOMER, "customer_notes": "x"}, "customer_notes"),        # near miss of the allowed names
    ({**CUSTOMER, "customer": {}}, "customer"),
    ({**CUSTOMER, "archived": True}, "archived"),
    ({"title": "Plain", "customer_status": "x"}, "customer_status"),
])
def test_every_other_unknown_field_is_still_refused_and_nothing_is_invoked(body, field):
    handler = Recorder()
    with pytest.raises(DomainError) as error:
        handler.business_post(None, object(), "business-1", ["task"], body)
    assert error.value.code == "validation_error" and dict(error.value.field_errors) == {field: "unknown field"}
    assert handler.calls == []


def test_a_title_is_still_required():
    handler = Recorder()
    with pytest.raises(DomainError) as error:
        handler.business_post(None, object(), "business-1", ["task"], {k: v for k, v in CUSTOMER.items() if k != "title"})
    assert dict(error.value.field_errors) == {"title": "required"} and handler.calls == []
