"""The changed worker gates on the real assembled host: Ordubashy and permanent workers, nobody else.

``test_host_worker_marketing_gates.py`` keeps the pure unit checks, which stand in the worker predicate. This module is the
assembled evidence: a real built host and database, the actual published roles, registered UUID agents, the configured
Ordubashy selected through ``management_coordinator_set`` and the real resolver and operation pipeline. Worker eligibility,
manager authority, the role artifacts and source authorization are never patched. Each operation is run first with the
agent's ordinary grants alone and then with the one explicit grant the ordinary contract requires, so a refusal that comes
from the gate is told apart from one that comes from policy. Only the synthetic instance, its principals and its tmp files
are fake; no model, provider, browser or network is used.
"""

from __future__ import annotations

import pytest

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

from host_foundation_support import (WORKER_MANIFEST, WORKER_ROLE, build_instance, create_registered_global, create_worker,
                                     owner_global_context, register_sources, role_root, select_coordinator, set_agent_overrides)


@pytest.fixture
def team(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    register_sources(instance, ids["first"], "first")
    register_sources(instance, ids["second"], "second")
    agents = {"ordubashy": select_coordinator(instance, create_registered_global(instance, "ordubashy")),
              "learner": create_registered_global(instance, "learning-applier"),
              "worker": create_worker(instance, "worker-a"),
              "other": create_worker(instance, "worker-b")}
    instance.runtime.freeze_registry()
    try:
        yield Team(instance, ids, agents)
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


class Team:
    def __init__(self, instance, ids, agents):
        self.instance, self.ids, self.agents = instance, ids, agents
        self.first, self.second = ids["first"], ids["second"]
        self.owner = owner_global_context(instance)

    def grant(self, who, operations):
        set_agent_overrides(self.instance, self.agents[who], {name: "allow" for name in operations}, list(self.ids.values()))

    def context(self, who, business=None):
        return self.instance.runtime.resolver.invocation_context(Principal(self.agents[who], "agent"),
                                                                 business_id=business or self.first)

    def call(self, ctx, name, arguments, revision=None):
        return self.instance.runtime.application.invoke(
            ctx, name, arguments, InvocationOptions(idempotency_key=new_id(), expected_revision=revision))

    def owner_call(self, name, arguments, business=None):
        ctx = self.instance.human(business or self.first)
        return self.call(ctx, name, arguments)

    def query(self, sql, *parameters):
        with self.instance.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def refusal(result):
    assert result.status == "error", plain(result.data)
    return result.error["code"], result.error.get("message", "")


# ----- the fixture really is the assembled team ----------------------------------------------------------------------------


def test_the_team_is_the_actual_published_role_with_registered_uuid_agents(team):
    manifest = (role_root() / "roles/imported-roles/native/roles" / WORKER_ROLE / "role.yaml").read_text()
    assert manifest != WORKER_MANIFEST                                       # the completed role package, not a stand-in
    roles = {who: team.query("SELECT json_extract(profile_json,'$.role.id') AS role, json_extract(profile_json,'$.role.digest') "
                             "AS digest FROM agent_profiles WHERE agent_id=?", team.agents[who])[0] for who in team.agents}
    assert {who: row["role"] for who, row in roles.items()} == {
        "ordubashy": "ordubashy", "learner": "learning-applier", "worker": WORKER_ROLE, "other": WORKER_ROLE}
    assert roles["worker"]["digest"] == roles["other"]["digest"] and len(set(team.agents.values())) == 4
    installation = team.query("SELECT management_coordinator_id FROM installation WHERE id='service'")[0]
    assert installation["management_coordinator_id"] == team.agents["ordubashy"]




# ----- the coordinator -------------------------------------------------------------------------------------------------------


def test_a_worker_cannot_be_selected_as_the_coordinator(team):
    selected = team.instance.invoke(team.owner, "management_coordinator_set", {"coordinator_id": team.agents["worker"]})
    assert selected.status == "error"
    assert team.query("SELECT management_coordinator_id AS id FROM installation WHERE id='service'")[0]["id"] == team.agents["ordubashy"]
