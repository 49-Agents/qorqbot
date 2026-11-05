"""Business archive is urgent owner review, not deletion; restore returns the business resources and no agent.

Contract: ``business_archive`` is proposed in Global by the owner or the registered Ordubashy and is always a human Signoff (an
``allow`` override cannot skip it). Approval hides the business from normal discovery, resets its terminal sessions once and keeps
every record, folder and agent identity; a changed business or business-home identity, a rejection or a failed reset leaves it
visible. ``business_archived_list`` and ``business_restore`` are owner-only; restore needs the current revision, returns only the
business and creates, reactivates or rewrites no agent. The transport's ``reset_business_for_archive`` clears only the archived
business's sessions, idempotently, and never launches an agent.

Real: the stock host assembly (``build_runtime`` with its policy, application, Signoff review and executor), the registered
Ordubashy, ``business_create``/``business_archive``/``business_restore``/``business_archived_list`` and the real ``TerminalService``
reset. Synthetic: the tmp instance, the injected terminal reset (it records), the fake tmux broker, and the explicitly HISTORICAL
business-home identity seeded only for the changed-snapshot case. No agent, provider, model, network or operational data is used."""

from types import SimpleNamespace

import pytest

pytest.importorskip("qorqut_context")

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain, utc_now

from bazarlyk.transport.terminals import TerminalService
from context_completion_support import build
from host_foundation_support import create_worker, grant_capabilities, historical_business_agent, make_paths, set_agent_overrides

RATIONALE = "Owner no longer wants this business in active work."


@pytest.fixture
def host(tmp_path):
    host = build(tmp_path, businesses=("first", "second"))
    host.resets = []
    host.runtime.domains.archive.reset_terminals = lambda business, agents: host.resets.append((business["id"], agents))
    host.worker = create_worker(host.instance, "plain")
    set_agent_overrides(host.instance, host.coordinator, {"business_archive": "allow"}, list(host.ids.values()))
    return host


def revision(host, slug="first"):
    return host.query("SELECT revision FROM businesses WHERE id=?", host.ids[slug])[0]["revision"]


def business(host, slug="first"):
    return host.query("SELECT * FROM businesses WHERE id=?", host.ids[slug])[0]


def propose(host, ctx, slug="first", key=None):
    return host.call(ctx, "business_archive", {"business_id": host.ids[slug], "rationale": RATIONALE}, key=key,
                     expected_revision=revision(host, slug))


def proposed(host, ctx, slug="first", key=None):
    result = propose(host, ctx, slug, key)
    assert result.status == "signoff_required", (result.status, result.error)
    return result.signoff


def detail(host, signoff):
    result = host.runtime.signoffs.get(host.human(None), signoff["id"])
    assert result.status == "ok", result.error
    return plain(result.data["signoff"])


def decide(host, signoff, decision="approve"):
    row = detail(host, signoff)
    return host.runtime.signoffs.review(host.human(None), row["id"], decision=decision, proposal_revision=row["proposal_revision"],
                                        payload_hash=row["payload_hash"], expected_status_revision=row["status_revision"],
                                        idempotency_key=new_id(), feedback="Not now." if decision == "reject" else None)


def settled(host, signoff):
    host.runtime.signoffs.run_pending()
    return detail(host, signoff)["state"]


def profiles(host):
    return host.query("SELECT * FROM agent_profiles ORDER BY agent_id")


def code(result):
    assert result.status == "error", plain(result.data)
    return result.error["code"]


# ---- proposal, approval and discovery ---------------------------------------------------------------------------------------------


def test_the_archive_tool_names_global_as_the_scope_of_the_proposal_and_its_review(host):
    descriptor = host.runtime.registry.get("business_archive")
    assert descriptor.scope_kinds == frozenset({"global"}) and descriptor.supports_signoff
    assert "remain in Global" in descriptor.input_schema["properties"]["business_id"]["description"]


@pytest.mark.parametrize("caller", ["owner", "ordubashy"])
def test_archive_requires_urgent_approval_and_removes_normal_discovery(host, caller):
    ctx = host.human(None) if caller == "owner" else host.agent(None)
    before, profiles_before = business(host), profiles(host)
    key = new_id()
    signoff = proposed(host, ctx, key=key)
    assert propose(host, ctx, key=key).signoff["id"] == signoff["id"]                 # the same key replays the same proposal
    pending = detail(host, signoff)
    assert pending["attention"]["urgent"] and pending["attention"]["notify"] and pending["review_business_id"] is None
    assert business(host) == before and host.resets == []                           # nothing happens before the owner decides
    assert decide(host, signoff).status == "accepted"
    assert settled(host, signoff) == "applied", detail(host, signoff)
    saved = business(host)
    assert saved["archived_at"] and saved["config_json"] == before["config_json"]
    assert saved["workspace"] == before["workspace"] and saved["hq_path"] == before["hq_path"]
    assert host.resets == [(before["id"], [])]
    host.runtime.signoffs.run_pending()
    assert len(host.resets) == 1 and profiles(host) == profiles_before                # idempotent; no identity changed
    # Normal discovery no longer reaches it, for the owner and for every global agent, and a selection of it is refused.
    assert host.ids["first"] not in host.runtime.resolver.invocation_context(Principal(host.coordinator, "agent")).allowed_business_ids
    assert host.ids["second"] in host.runtime.resolver.invocation_context(Principal(host.coordinator, "agent")).allowed_business_ids
    assert code(host.call(host.human("first"), "tasks_list", {})) == "business_archived"
    assert [row["slug"] for row in host.data(host.human(None), "business_archived_list")["items"]] == ["first"]


def test_archive_rejection_does_not_reset_terminals_or_change_records(host):
    before = business(host)
    signoff = proposed(host, host.agent(None))
    decide(host, signoff, "reject")
    host.runtime.signoffs.run_pending()
    assert detail(host, signoff)["state"] == "rejected" and business(host) == before and host.resets == []


@pytest.mark.parametrize("change", ["business", "business_home_identity"])
def test_changed_archive_target_requires_new_approval(host, change):
    historical = historical_business_agent(host.instance, host.ids["first"], status="inactive")
    signoff = proposed(host, host.agent(None))
    if change == "business":
        with host.database.transaction() as uow:
            uow.repository("businesses").update(host.ids["first"], {"name": "Changed"}, expected_revision=revision(host))
    else:
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET name='renamed' WHERE agent_id=?", (historical,))
    decide(host, signoff)
    host.runtime.signoffs.run_pending()
    assert detail(host, signoff)["state"] == "conflicted"
    assert business(host)["archived_at"] is None and host.resets == []


def test_archive_failure_is_reported_and_leaves_business_visible(host):
    def failed(business, agents):
        raise DomainError("operation_unavailable", "Terminal reset unavailable")
    host.runtime.domains.archive.reset_terminals = failed
    signoff = proposed(host, host.agent(None))
    decide(host, signoff)
    host.runtime.signoffs.run_pending()
    assert detail(host, signoff)["state"] != "applied"
    assert business(host)["archived_at"] is None


def test_archive_without_an_installed_terminal_reset_applies_nothing(host):
    host.runtime.domains.archive.reset_terminals = None
    signoff = proposed(host, host.agent(None))
    decide(host, signoff)
    host.runtime.signoffs.run_pending()
    assert detail(host, signoff)["state"] != "applied" and business(host)["archived_at"] is None


def test_archive_allow_overrides_cannot_skip_review_and_an_ungranted_agent_cannot_propose(host):
    assert propose(host, host.human(None)).status == "signoff_required"
    assert propose(host, host.agent(None)).status == "signoff_required"                # the explicit allow override
    worker = host.runtime.resolver.invocation_context(Principal(host.worker, "agent"))
    assert code(propose(host, worker)) in {"forbidden", "policy_denied"}
    set_agent_overrides(host.instance, host.worker, {"business_archive": "allow"}, list(host.ids.values()))
    worker = host.runtime.resolver.invocation_context(Principal(host.worker, "agent"))
    assert propose(host, worker).status in {"signoff_required", "error"}               # never an applied change without review
    assert business(host)["archived_at"] is None and host.resets == []


def test_archived_business_is_absent_from_ordubashy_scope_without_rewriting_its_profile(host):
    profile = host.query("SELECT profile_json FROM agent_profiles WHERE agent_id=?", host.coordinator)[0]
    signoff = proposed(host, host.agent(None))
    decide(host, signoff)
    assert settled(host, signoff) == "applied", detail(host, signoff)
    context = host.runtime.resolver.invocation_context(Principal(host.coordinator, "agent"))
    assert context.allowed_business_ids == frozenset({host.ids["second"]})
    assert host.query("SELECT profile_json FROM agent_profiles WHERE agent_id=?", host.coordinator)[0] == profile
    # A business created afterwards is reached through the same derived scope, with no profile write. Ordubashy is the creator
    # (it needs its creation capability and grant, which are profile edits made before the profile is compared).
    grant_capabilities(host.instance, host.coordinator, "coordinate_businesses")
    # Only the active business scopes the owner's edit: the helper picks ``sorted(businesses)[0]`` as the request's business, which
    # would be the archived one for about half of the random identities.
    set_agent_overrides(host.instance, host.coordinator, {"business_archive": "allow", "business_create": "allow"}, [host.ids["second"]])
    profile = host.query("SELECT profile_json FROM agent_profiles WHERE agent_id=?", host.coordinator)[0]
    created = host.call(host.agent(None), "business_create", {
        "slug": "after-archive", "name": "After", "summary": "Help repair shops."})
    assert created.status == "ok", (created.status, created.error)
    after = host.query("SELECT id FROM businesses WHERE slug='after-archive'")[0]["id"]
    assert host.runtime.resolver.invocation_context(Principal(host.coordinator, "agent")).allowed_business_ids == frozenset(
        {host.ids["second"], after})
    assert host.query("SELECT profile_json FROM agent_profiles WHERE agent_id=?", host.coordinator)[0] == profile


# ---- archived listing and restore -------------------------------------------------------------------------------------------------


def archived(host, slug="first"):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=?, archived_by=? WHERE id=?", (utc_now(), host.owner, host.ids[slug]))


def owner_global(host):
    return InvocationContext(Principal(host.owner, "human"), allowed_business_ids=frozenset(), source="dashboard",
                             global_capabilities=host.human(None).global_capabilities)


def test_archived_listing_is_owner_only_and_feeds_restore(host):
    archived(host)
    saved = business(host)
    listed = host.data(owner_global(host), "business_archived_list")["items"]
    assert listed == [{"business_id": saved["id"], "slug": "first", "name": saved["name"], "revision": saved["revision"],
                       "archived_at": saved["archived_at"]}]
    for ctx in (host.agent(None), host.runtime.resolver.invocation_context(Principal(host.worker, "agent"))):
        assert code(host.call(ctx, "business_archived_list")) in {"forbidden", "policy_denied"}
    assert code(host.call(ctx, "business_restore", {"business_id": saved["id"]}, expected_revision=saved["revision"])) in {
        "forbidden", "policy_denied"}
    assert business(host)["archived_at"] is not None
    agents_before = profiles(host)
    restored = host.call(owner_global(host), "business_restore", {"business_id": saved["id"], "rationale": "Back on the menu."},
                         expected_revision=listed[0]["revision"])
    assert restored.status == "ok", (restored.status, restored.error)
    assert plain(restored.data) == {"business_id": saved["id"], "restored": True}
    assert business(host)["archived_at"] is None and business(host)["archived_by"] is None
    assert host.data(owner_global(host), "business_archived_list")["items"] == []
    assert profiles(host) == agents_before                                              # restore writes no agent
    assert host.ids["first"] in host.runtime.resolver.invocation_context(Principal(host.coordinator, "agent")).allowed_business_ids


def test_the_restore_proposal_returns_the_project_to_the_project_menu(host):
    archived(host)
    saved = business(host)
    descriptor = host.runtime.registry.get("business_restore")
    with host.database.read() as uow:
        plan = descriptor.prepare(owner_global(host), {"business_id": saved["id"]}, InvocationOptions(expected_revision=saved["revision"]), uow)
    effects = " ".join(plan.preview["effects"])
    assert "to the active project menu." in effects and "business menu" not in effects
    assert "Every active global agent reaches the project again" in effects and plan.preview["title"].startswith("Restore project: ")


def test_restore_needs_an_archived_business_the_current_revision_and_the_owner_in_global(host):
    owner = owner_global(host)
    assert code(host.call(owner, "business_restore", {"business_id": host.ids["first"]}, expected_revision=revision(host))) == "business_not_archived"
    archived(host)
    assert code(host.call(owner, "business_restore", {"business_id": host.ids["first"]}, expected_revision=revision(host) + 1)) == "revision_conflict"
    assert code(host.call(host.human("first"), "business_restore", {"business_id": host.ids["first"]}, expected_revision=revision(host))) in {
        "forbidden", "scope_required", "invalid_scope", "business_archived"}
    # A business the installation owner does not own is not restorable, and an unknown one does not exist.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET is_owner=0 WHERE business_id=?", (host.ids["first"],))
    assert code(host.call(owner, "business_restore", {"business_id": host.ids["first"]}, expected_revision=revision(host))) == "forbidden"
    assert host.data(owner, "business_archived_list")["items"] == []
    assert code(host.call(owner, "business_restore", {"business_id": new_id()})) == "not_found"
    assert business(host)["archived_at"] is not None


def test_a_restore_plan_with_a_roster_change_is_refused(host):
    """A plan carrying a retired roster restoration (an older prepared restore) is invalid, not applied."""
    from qorqut_operations.projects.archive import BusinessArchive
    plan = SimpleNamespace(changes=[SimpleNamespace(action="restore_roster")])
    with pytest.raises(DomainError) as error:
        BusinessArchive.validate_restore(None, plan, None)
    assert error.value.code == "invalid_plan"


# ---- the trusted terminal reset ---------------------------------------------------------------------------------------------------


class Broker:
    """The fake tmux broker: sessions are names, and every kill and ensure is recorded."""

    def __init__(self, name):
        self.name, self.sessions, self.kill_calls, self.stop_calls, self.ensure_calls = name, set(), [], [], []
        self.mirrors = {}

    @staticmethod
    def tmux_available():
        return True

    def session_name(self, slug, name):
        return f"{self.name}:{slug}:{name}"

    def session_prefix(self, slug):
        return f"{self.name}:{slug}:"

    def ensure_session(self, session, shell, cwd, **kwargs):
        self.sessions.add(session)
        self.ensure_calls.append(session)

    def list_sessions(self, prefix):
        return [session for session in self.sessions if session.startswith(prefix)]

    def kill_session(self, session):
        self.kill_calls.append(session)
        if session not in self.sessions:
            return False
        self.sessions.remove(session)
        return True

    def stop(self, key):
        self.stop_calls.append(key)
        return self.mirrors.pop(key, None) is not None


def test_terminal_reset_is_scoped_idempotent_and_does_not_launch_agents(tmp_path):
    broker = Broker("host")
    paths = make_paths(tmp_path)
    terminal = TerminalService(SimpleNamespace(root=tmp_path, workspace=tmp_path, paths=paths), broker, service_url="http://localhost/mcp")
    business = {"id": "one", "slug": "one", "hq_path": str(paths.business_hq("one"))}
    agents = [{"agent_id": "a", "name": "coder", "workspace": str(tmp_path / "ignored"), "home_business_id": "one"}]
    broker.sessions.update({"host:one:coder", "host:two:coder", "host:global:ordubashy"})
    terminal.reset_business_for_archive(business, agents)
    terminal.reset_business_for_archive(business, agents)
    assert broker.sessions == {"host:two:coder", "host:global:ordubashy"}
    assert broker.kill_calls == ["host:one:coder"]
    assert broker.ensure_calls == []
