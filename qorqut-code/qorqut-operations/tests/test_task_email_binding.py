"""A historical Email Task keeps its conversation binding as read-only data; ordinary writes never name it."""

import pytest

from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.types import apply_attributes, attributes, validate_attributes

CONVERSATION = "11111111-1111-4111-8111-111111111111"


def historical(**extra):
    return {"id": "t", "title": "Old", "fields_json": {"$task": {"task_type": "email", "overview": "", "email_conversation_id": CONVERSATION}, **extra}}


def test_an_edit_that_keeps_the_type_keeps_the_binding():
    task = historical()
    apply_attributes(task, {"overview": "Where it stands"})
    assert attributes(task)["task_type"] == "email" and attributes(task)["email_conversation_id"] == CONVERSATION


def test_changing_the_type_away_from_email_clears_the_binding_without_naming_it():
    task = historical()
    apply_attributes(task, {"task_type": "story"})
    assert attributes(task)["email_conversation_id"] is None
    validate_attributes(task)                                         # and the Task is valid as an ordinary Story


def test_naming_a_binding_for_a_non_email_type_is_still_a_contradiction():
    task = historical()
    apply_attributes(task, {"task_type": "story", "email_conversation_id": CONVERSATION})
    with pytest.raises(DomainError) as refused:
        validate_attributes(task)
    assert refused.value.code == "validation_error"


def test_an_email_task_without_a_binding_is_still_refused():
    task = {"id": "t", "title": "New", "fields_json": {}}
    apply_attributes(task, {"task_type": "email"})
    with pytest.raises(DomainError):
        validate_attributes(task)
