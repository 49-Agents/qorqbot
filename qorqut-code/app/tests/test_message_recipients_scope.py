"""``agents_message_recipients`` lists each other active agent once for the selected scope; ``agents_message_send`` has no
delivery argument."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from types import SimpleNamespace
import unittest

from jsonschema import Draft202012Validator

from qorqut_operations.contracts import DomainError
from qorqut_operations.inbox.messages import Messages

ME = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
SECOND_PROJECT = "55555555-5555-4555-8555-555555555555"


@dataclass(frozen=True)
class Ctx:
    principal_id: str
    project_id: str | None
    allowed_project_ids: frozenset


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=()):
        return [row for row in self.rows if row["agent_id"] != params[0]]


class Projects:
    def __init__(self, archived=()):
        self.archived = set(archived)

    def get(self, identifier):
        return {"slug": "slug-" + identifier[:2], "archived_at": "x" if identifier in self.archived else None}


class Uow:
    def __init__(self, archived=()):
        profile = lambda role: json.dumps({"role": {"id": role}})
        self.connection = Rows([
            {"agent_id": ME, "name": "Me", "profile_json": profile("ordubashy")},
            {"agent_id": OTHER, "name": "Worker", "profile_json": profile("global-worker")},
            {"agent_id": THIRD, "name": "Applier", "profile_json": profile("learning-applier")},
        ])
        self._projects = Projects(archived)

    def repository(self, name):
        return self._projects


def build():
    host = SimpleNamespace(**{name: (lambda *a, **k: None) for name in (
        "owner_directed_target", "binding", "validate_recipient", "attribution", "history_scope", "forward_owner_message",
        "retirement_backlog")})
    app = SimpleNamespace(current_context=lambda ctx, uow: ctx)
    messages = Messages(SimpleNamespace(app=app), host=host)

    def peer(ctx, identity, uow, **kwargs):
        return SimpleNamespace(principal_id=identity, principal=SimpleNamespace(display_name="D-" + identity[:1]))
    messages.peer = peer
    return messages


class RecipientsTests(unittest.TestCase):
    def test_a_selected_project_lists_each_agent_once_with_that_project(self):
        ctx = Ctx(ME, PROJECT, frozenset({PROJECT, SECOND_PROJECT}))
        result = build().recipients(ctx, {}, None, Uow())
        self.assertEqual([item["agent_id"] for item in result["items"]], [OTHER, THIRD])
        self.assertEqual({item["project_id"] for item in result["items"]}, {PROJECT})
        self.assertEqual(result["projects"], [{"project_id": PROJECT, "project_slug": "slug-44"}])

    def test_global_lists_each_agent_once_without_a_project(self):
        ctx = Ctx(ME, None, frozenset({PROJECT, SECOND_PROJECT}))
        result = build().recipients(ctx, {}, None, Uow())
        self.assertEqual([item["agent_id"] for item in result["items"]], [OTHER, THIRD])
        self.assertEqual({item["project_id"] for item in result["items"]}, {None})
        self.assertEqual(result["projects"], [{"project_id": None, "project_slug": None}])

    def test_the_role_filter_narrows_the_list(self):
        result = build().recipients(Ctx(ME, None, frozenset()), {"role_id": "global-worker"}, None, Uow())
        self.assertEqual([item["agent_id"] for item in result["items"]], [OTHER])

    def test_an_archived_selected_project_lists_nothing(self):
        result = build().recipients(Ctx(ME, PROJECT, frozenset({PROJECT})), {}, None, Uow(archived=[PROJECT]))
        self.assertEqual(result, {"items": [], "projects": []})

    def test_an_agent_that_cannot_be_addressed_is_left_out(self):
        messages = build()

        def peer(ctx, identity, uow, **kwargs):
            if identity == OTHER:
                raise DomainError("not_found", "no")
            return SimpleNamespace(principal_id=identity, principal=SimpleNamespace(display_name="x"))
        messages.peer = peer
        result = messages.recipients(Ctx(ME, None, frozenset()), {}, None, Uow())
        self.assertEqual([item["agent_id"] for item in result["items"]], [THIRD])


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.found = {}
        messages = build()
        messages.app = SimpleNamespace(registry=SimpleNamespace(register=lambda d: self.found.__setitem__(d.name, d)))
        messages.register({"supports_signoff": False})

    def errors(self, name, value):
        return list(Draft202012Validator(self.found[name].input_schema).iter_errors(value))

    def test_send_refuses_a_delivery_argument(self):
        self.assertEqual(self.errors("agents_message_send", {"message": "hi", "to_agent_id": OTHER}), [])
        self.assertTrue(self.errors("agents_message_send", {"message": "hi", "to_agent_id": OTHER, "delivery": "inbox"}))

    def test_recipients_takes_scope_and_role_only(self):
        self.assertEqual(self.errors("agents_message_recipients", {"scope": "global", "role_id": "global-worker"}), [])
        self.assertTrue(self.errors("agents_message_recipients", {"all_projects": True}))


if __name__ == "__main__":
    unittest.main()
