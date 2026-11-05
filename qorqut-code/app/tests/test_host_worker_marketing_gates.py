"""Marketing gates answer to Ordubashy/worker eligibility, never to a Marketing role (v1.2 Ordubashy team, skills).

Real: the gate functions of the email plugin defaults and the shared role text. Faked: only the Foundation worker predicate
``bazarlyk.agents.worker_access.is_worker`` (its module is supplied by the Foundation change, so each case registers a stand-in
that answers from a set of synthetic agent IDs). No operational data, database or provider is used.

UNIT TIER ONLY. These stand-ins are not evidence of the assembled authority. The same worker gates run against the real
assembled host, the actual published roles, registered UUID agents, the configured Ordubashy and the real resolver in
``test_host_worker_gates_assembled.py``."""

import sqlite3
import sys
import types
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError

from bazarlyk.agents import plugin_defaults, role_workflow

# FOUNDER is the retired role: an identity still pinned to it authorizes nothing.
ORDUBASHY, FOUNDER, WORKER, OTHER_WORKER, LEARNING, BUSINESS = "ord", "founder", "worker-1", "worker-2", "learner", "biz"
ROLES = {ORDUBASHY: "ordubashy", FOUNDER: "founder", WORKER: "global-worker", OTHER_WORKER: "global-worker",
         LEARNING: "learning-applier"}


@pytest.fixture
def workers(monkeypatch):
    """The active workers the stand-in predicate recognizes."""
    active = {WORKER, OTHER_WORKER}
    module = types.ModuleType("bazarlyk.agents.worker_access")
    module.is_worker = lambda connection, agent_id: agent_id in active
    monkeypatch.setitem(sys.modules, "bazarlyk.agents.worker_access", module)
    return active


def agent(identity, *, delegated=False):
    return SimpleNamespace(principal_kind="agent", principal_id=identity, home_business_id=None, business_id=BUSINESS,
                           delegation_assignment_id="assignment" if delegated else None,
                           role_artifact_ref=SimpleNamespace(id=ROLES[identity]))


PERSON = SimpleNamespace(principal_kind="human", principal_id="person", home_business_id=None, business_id=BUSINESS,
                         delegation_assignment_id=None, role_artifact_ref=None)
UOW = SimpleNamespace(connection=None)


def refused(call, *args, **kwargs):
    with pytest.raises(DomainError) as error:
        call(*args, **kwargs)
    assert error.value.code == "forbidden"


# Plugin defaults and sending mailboxes.

def test_a_new_worker_receives_no_plugin_and_other_globals_keep_the_email_toolkit():
    ecosystem, business = {"kind": "ecosystem"}, {"kind": "business"}
    assert plugin_defaults.default_plugin_ids("global-worker", ecosystem) == ()
    for role in ("ordubashy", "learning-applier"):
        assert plugin_defaults.default_plugin_ids(role, ecosystem) == (plugin_defaults.EMAIL_PLUGIN,)
    assert plugin_defaults.default_plugin_ids("global-marketing", ecosystem) == (plugin_defaults.EMAIL_PLUGIN,)
    assert plugin_defaults.default_plugin_ids("global-worker", business) == ()
    assert not hasattr(plugin_defaults, "MARKETING_ROLES")


def test_only_ordubashy_sends_from_every_registered_mailbox_and_other_grants_use_their_list():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE email_mailboxes (id TEXT, business_id TEXT, purpose TEXT)")
        connection.executemany("INSERT INTO email_mailboxes VALUES (?,?,?)", [
            ("business", "one", "business_mail"), ("archive", "one", "outreach_archive"),
            ("other", "two", "business_mail"), ("owner", None, "business_mail")])
        send = plugin_defaults.sending_mailboxes
        assert send(connection, "one", "ordubashy", {}) == ["archive", "business"]
        assert send(connection, "one", "ordubashy", {"mailboxes": ["business"]}) == ["archive", "business"]
        for role in ("global-worker", "global-marketing", "learning-applier", None):
            assert send(connection, "one", role, {}) == []
            assert send(connection, "one", role, None) == []
            assert send(connection, "one", role, {"mailboxes": ["business"]}) == ["business"]




# Shared role text.

def test_shared_role_text_teaches_current_work_team_and_notices_without_a_marketing_peer():
    from importlib.resources import files
    text = files(role_workflow.__package__).joinpath("skills/role/SKILL.md").read_text()
    for source in (role_workflow.INSTRUCTIONS, text):
        flat = " ".join(source.replace("`", "").split())
        for phrase in ("A permanent worker keeps one current item, not a list",
                       "a description of at most 100 words, or both",
                       "operations_describe has the agent_work_update fields",
                       "Give each worker one atomic assignment at a time and choose its next",
                       "worker rows carry current_work" if source is role_workflow.INSTRUCTIONS else "current_work shows what that worker is doing",
                       "reaches Ordubashy in full as a forwarded notice",
                       "not a second assignment",
                       # Existing session-list guidance for every other agent stays.
                       "Prefer patch actions add/replace/remove" if source is role_workflow.INSTRUCTIONS else "Prefer `patch` actions"):
            assert phrase in flat or phrase.replace("`", "") in flat, phrase
        assert "Founder and Marketing" not in flat and "Marketing peer" not in flat


def test_a_worker_reaches_the_owner_only_through_ordubashy_in_the_shared_guidance():
    from importlib.resources import files
    text = files(role_workflow.__package__).joinpath("skills/role/SKILL.md").read_text()
    for source in (role_workflow.INSTRUCTIONS, text):
        flat = " ".join(source.replace("`", "").split())
        for phrase in ("A permanent worker instead sends every question, decision, review request, access request and "
                       "normal completion report to Ordubashy, whatever a later section says about asking the owner "
                       "(email decisions, UI review, input Signoffs), and answers the owner only to their actual direct message",
                       "its exact-action approvals stay as their tools require",
                       "Globals other than a permanent worker",
                       "or an Ordubashy assignment carrying the owner's actual authorization, is yours to carry out",
                       "Assignments, subscriptions and labels never expand permissions or approval authority"):
            assert phrase in flat, phrase
        # Existing Founder and Learning Applier behavior is kept for every other Global.
        assert ("signoffs_input_create when the owner's attention is needed" if source is text else
                "sends the owner one concise direct reply with the result") in flat


def test_inbox_instructions_describe_the_permanent_worker_route_without_a_fixed_roster():
    from bazarlyk.inbox.defaults import INBOX_INSTRUCTIONS
    flat = " ".join(INBOX_INSTRUCTIONS.split())
    for phrase in ("Global agents work across every active project",
                   "takes assignments from Ordubashy, who manages the team, and sends him its questions, access requests and completion reports",
                   "or an assignment from Ordubashy for work the owner authorized (no further direct message to you needed)",
                   "Following or being assigned a task does not grant authority"):
        assert phrase in flat, phrase
    assert "Global Marketing" not in flat and "Ordubashy, Founder, Global" not in flat
