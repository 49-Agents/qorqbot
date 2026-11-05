"""Isolated real backend for frontend acceptance. No production paths or models."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace

import uvicorn

from qorqut_operations.contracts import InvocationOptions, new_id
from bazarlyk.service.web import create_app
from bazarlyk.transport.compatibility import DashboardCompatibility
from inbox_support import claim
from learning_history_seed import seed_learning_history
from test_agent_configuration import REPO, PATHS
from test_service_transports import OWNER_TOKEN, ServiceFixture


def build_fixture(root, origin, *, dashboard_root=None, learnings=False, global_signoffs=False, personal_tasks=False, tables=False, global_tables=False, inbox=False, notes_only=False, many_signoffs=False, products=False):
    class AcceptanceModel:
        config = SimpleNamespace(model="deepseek-v4-flash")

        def chat(self, messages, **kwargs):
            return {"content": "Retain source evidence for the fixture pilot."}, {"prompt_tokens": 10, "completion_tokens": 10}

    fixture = ServiceFixture(root, full=True, model_client_factory=lambda business: AcceptanceModel())
    if notes_only:
        fixture.runtime.background.start = lambda: None
        from qorqut_context.integration.reading import PublishedContextText
        fixture.runtime.resolver.context_home = PublishedContextText(fixture.runtime.domains.shared_context).home_text
        app = create_app(fixture.runtime, dashboard_root=dashboard_root or PATHS.component("dashboard"),
            origins=[origin], authentication=fixture.authentication,
            compatibility=DashboardCompatibility(fixture.runtime, background=fixture.runtime.background))
        return app, {"url": origin, "root": str(fixture.fixture.root)}

    if tables or global_tables:
        subprocess.run(["git", "init", "-q", str(fixture.fixture.root)], check=True)
    sessions = fixture.fixture.hq / "agents/business-agent/sessions"
    sessions.mkdir(exist_ok=True)
    session_id = new_id()
    (sessions / "fixture.jsonl").write_text(json.dumps({"type": "user", "sessionId": session_id, "uuid": new_id(),
        "timestamp": "2026-09-08T00:00:00Z", "message": {"content": "Retain source evidence for the fixture pilot."}}) + "\n")
    owner, runtime = fixture.fixture.ctx, fixture.runtime
    with runtime.database.transaction() as uow:
        business = uow.repository("businesses").get(fixture.fixture.business_id)
        uow.repository("businesses").update(business["id"], {"config_json": {"accounts": {"reddit": [
            {"id": "reddit-founder", "handle": "founder", "manual": True}]}}}, expected_revision=business["revision"])
    reddit = fixture.agent
    fixture.fixture.enable_reddit(reddit)
    marketing = runtime.application.invoke(owner, "signoffs_platform_submit", {"plugin_id": "reddit-organic", "tool": "reddit.post",
        "ability_agent_id": reddit.id, "why": "Share tested evidence.", "payload": {"subreddits": ["ClaudeAI"],
            "title": "A fixture workflow", "body": "Exact fixture evidence.", "flair": "Built with Claude"}},
        InvocationOptions(idempotency_key=new_id()))
    if marketing.status != "signoff_required":
        raise RuntimeError("Browser fixture platform Signoff was not created: " + json.dumps(marketing.to_dict()))
    bulk_total = bulk_pending = 0
    if many_signoffs:
        # Reproduces the volume /live and the Signoffs pane actually see on a
        # business with real history: a handful pending among a much larger
        # resolved majority. Rejection (not approval) resolves the bulk here
        # because it's a single cheap owner decision with no background job
        # to run, but signoffs/service.py::_dto() enriches a resolved
        # platform_action row (platform_view + job lookups) the same way
        # regardless of which resolved state it ended in, so this exercises
        # the exact per-row cost the pending-only fetch exists to skip.
        bulk_total, bulk_pending = 165, 5
        for i in range(bulk_total):
            submission = runtime.application.invoke(owner, "signoffs_platform_submit", {"plugin_id": "reddit-organic",
                "tool": "reddit.post", "ability_agent_id": reddit.id, "why": "Bulk fixture volume.",
                "payload": {"subreddits": ["ClaudeAI"], "title": f"Bulk fixture post {i}",
                    "body": "Exact fixture evidence.", "flair": "Built with Claude"}},
                InvocationOptions(idempotency_key=new_id()))
            if submission.status != "signoff_required":
                raise RuntimeError("Bulk fixture platform Signoff was not created: " + json.dumps(submission.to_dict()))
            if i >= bulk_pending:
                row = submission.signoff
                runtime.signoffs.review(owner, row["id"], decision="reject", proposal_revision=row["proposal_revision"],
                    payload_hash=row["payload_hash"], expected_status_revision=row["status_revision"],
                    idempotency_key=new_id(), feedback="Bulk fixture rejection.")
    runtime.application.invoke(owner, "tasks_create", {"title": "Review this week's evidence", "assignee_ids": [fixture.agent.id]},
        InvocationOptions(idempotency_key=new_id()))
    # Owners write business notes as business-owned strategy notes.
    business_note = {"target_business_id": fixture.fixture.business_id}
    runtime.application.invoke(owner, "notes_create", {"title": "Current focus", "body": "Measure a complete customer workflow.", **business_note},
        InvocationOptions(idempotency_key=new_id()))
    def context_document(operation, arguments):
        result = runtime.application.invoke(owner, operation, arguments, InvocationOptions(idempotency_key=new_id()))
        if result.status != "ok":
            raise RuntimeError("Browser fixture context was not created: " + json.dumps(result.to_dict()))
        return result.data["document"]

    context_document("notes_create", {"title": "Literal strategy fixture", "body": "- Literal dash\n\n⸻\nFree text <tag>.\n", **business_note})

    research = context_document("context_topics_create", {"title": "Pilot research", "slug": "research"})
    launch = context_document("context_topics_create", {"title": "Launch", "slug": "launch"})
    person = context_document("context_people_create", {"name": "Ada", "role": "Pilot sponsor"})
    graph_note = context_document("context_notes_create", {"title": "Pilot evidence", "body": "Ada confirmed the next pilot.",
        "topic_id": research["id"], "people_ids": [person["id"]], "also_topic_ids": [launch["id"]]})
    # The coordinator's profile edits explicitly require review, so browser
    # signoff tests still have a real protected action.
    fixture.fixture.update(fixture.manager, {'tool_policy': {'overrides': {'agents_update': 'signoff'}}})
    with runtime.database.read() as uow:
        manager = runtime.policy.context_for(fixture.manager.id, business_id=fixture.fixture.business_id,
                                             review_business_id=fixture.fixture.business_id, uow=uow)
        revision = runtime.resolver.profile(fixture.agent, connection=uow.connection)[0].revision
    pending = runtime.application.invoke(manager, "agents_update", {"agent_id": fixture.agent.id,
        "changes": {"assignment_brief": "Own the next small growth experiment."}},
        InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
    if pending.status != "signoff_required":
        raise RuntimeError("Browser fixture did not create its real pending signoff: " + json.dumps(pending.to_dict()))
    learning_details = {}
    if learnings:
        learning_ids = seed_learning_history(runtime.database, fixture.fixture.business_id,
                                             owner.principal_id, application=runtime.application)
        global_id, run_id, prior_id = new_id(), new_id(), new_id()
        evidence = {'session': 'signoff_response:fixture', 'run': run_id, 'turns': ['T1'],
                    'owner_quote': 'Keep answers scoped. <literal>',
                    'source': {'kind': 'signoff_response', 'actor_name': 'Owner', 'at': '2026-09-10T00:00:00Z'}}
        with runtime.database.transaction() as uow:
            uow.repository('learning_runs', business_id=None).insert({'id': run_id, 'result_json': {
                'cores': {evidence['session']: {'markdown': 'Frozen Global question and answer: <literal>'}}}})
            uow.repository('learning_candidates', business_id=None).insert({'id': global_id, 'candidate_json': {
                'text': 'Keep Global answers scoped.', 'scope': 'global', 'tag': 'rule', 'bucket': 'delta',
                'created': '2026-09-10T00:00:00Z', 'run': run_id, 'evidence': evidence, 'contradicts': [prior_id]}})
            uow.repository('learning_candidates', business_id=None).insert({'id': prior_id, 'candidate_json': {
                'text': 'Previously accepted Global rule', 'scope': 'global', 'tag': 'rule', 'bucket': 'delta'}})
            uow.repository('learning_decisions', business_id=None).insert({'candidate_id': prior_id, 'state': 'accepted', 'decided_by': owner.principal_id})
        question_id, answer_id = new_id(), new_id()
        old_question = {'id': question_id, 'text': 'How much was refunded? <literal>', 'scope': 'global',
            'tag': 'question', 'bucket': 'context', 'run': run_id, 'evidence': evidence}
        with runtime.database.transaction() as uow:
            uow.repository('learning_candidates', business_id=None).insert({'id': question_id,
                'candidate_json': {**old_question, 'reconciled_into': answer_id}})
            uow.repository('learning_candidates', business_id=None).insert({'id': answer_id, 'candidate_json': {
                'text': 'The newsletter refund was $60.', 'scope': 'global', 'tag': 'fact', 'bucket': 'context',
                'created': '2026-09-10T01:00:00Z', 'run': run_id, 'evidence': evidence,
                'reconciliations': [{'kind':'answered','reason':'Owner answered later that day.',
                    'earlier':old_question,'automatic':True}]}})
        learning_details = {"learning_ids": {**learning_ids, 'global': global_id, 'answered':answer_id, 'question':question_id}}
        global_agent = fixture.fixture.create('global-learning-founder', 'ordubashy')
        chat_id = new_id()
        with runtime.database.transaction() as uow:
            uow.repository('learning_candidates', business_id=None).insert({'id': chat_id, 'candidate_json': {
                'text': 'For Project A, retain the weekly summary scope.', 'scope': 'agent_kind', 'tag': 'rule', 'bucket': 'delta',
                'observed': {'biz': None, 'agent': global_agent.id, 'agent_kind': 'ordubashy'},
                'created': '2026-09-13T00:00:00Z'}})
        learning_details['learning_ids']['chat'] = chat_id
        material = fixture.fixture.document('global-learning-material', 'Global filed evidence <literal>', 'context/fixture-material.md')
        with runtime.database.transaction() as uow:
            uow.repository('documents', business_id=None).update(material['id'],
                {'kind': 'context_note', 'metadata_json': {'title': 'Global filed material'}}, expected_revision=material['revision'])
    global_details = {}
    if global_signoffs:
        fixture.fixture.ctx = replace(fixture.fixture.ctx,
            global_capabilities=fixture.fixture.ctx.global_capabilities | {'manage_global_routines'})
        coordinator = fixture.fixture.create('ordubashy', 'ordubashy')
        proposer = runtime.resolver.invocation_context(coordinator)
        for suffix in ('approve', 'reject'):
            proposal = runtime.application.invoke(proposer, 'routines_create', {'scope': 'global', 'definition': {
                'name': 'Global proposal ' + suffix, 'program': 'repository.status', 'arguments': {},
                'schedule': '0 0 * * *', 'timezone': 'UTC', 'status': 'paused', 'target': 'each-business'}},
                InvocationOptions(idempotency_key=new_id()))
            if proposal.status != 'signoff_required':
                raise RuntimeError(str(proposal.to_dict()))
            global_details['global_' + suffix + '_id'] = proposal.signoff['id']
    table_details = {}
    if global_tables:
        # A Global table the Global Ordubashy asks to archive: agent archival always waits for the owner's Signoff.
        global_owner = replace(owner, business_id=None, review_business_id=None)
        def global_table_call(ctx, operation, arguments, revision=None):
            result = runtime.application.invoke(ctx, operation, {'scope': 'global', **arguments},
                InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
            if result.status not in ('ok', 'signoff_required'): raise RuntimeError(str(result.to_dict()))
            return result
        proposed = global_table_call(global_owner, 'tables_create', {'title': 'Retired vendor list',
            'columns': [{'id': 'name', 'name': 'Name', 'type': 'text'}]}).data['table']
        archive = global_table_call(runtime.resolver.invocation_context(fixture.manager), 'tables_archive',
            {'table_id': proposed['id'], 'rationale': 'The vendor list is no longer maintained.'}, revision=1)
        if archive.status != 'signoff_required': raise RuntimeError(str(archive.to_dict()))
        table_details = {'global_table_id': proposed['id'], 'global_table_archive_signoff_id': archive.signoff['id']}
    personal_details = {}
    if personal_tasks:
        other = new_id()
        hq = fixture.fixture.root / 'beta/hq'
        hq.mkdir(parents=True)
        with runtime.database.transaction() as uow:
            uow.repository('businesses').insert({'id': other, 'slug': 'beta', 'name': 'Beta Studio', 'workspace': str(hq.parent), 'hq_path': str(hq)})
            uow.repository('business_memberships', business_id=other).insert({'principal_id': owner.principal_id, 'is_owner': True})
        def personal_call(ctx, operation, arguments):
            result = runtime.application.invoke(ctx, operation, arguments, InvocationOptions(idempotency_key=new_id()))
            if result.status != 'ok': raise RuntimeError(str(result.to_dict()))
            return result.data
        with runtime.database.read() as uow:
            beta = runtime.policy.context_for(owner.principal_id, business_id=other, review_business_id=other, uow=uow)
        beta_story = personal_call(beta, 'stories_create', {'title': 'Beta launch', 'purpose': 'Coordinate the launch'})['story']
        widget_stories = [personal_call(owner, 'stories_create', {'title': title, 'purpose': 'Coordinate owner review'})['story']
                          for title in ('Widget launch', 'Widget research')]
        personal_call(owner, 'tasks_create', {'id': 'owner-review', 'title': 'Review Widget launch', 'assignee_ids': [owner.principal_id],
                                             'story_ids': [story['id'] for story in widget_stories]})
        personal_call(beta, 'tasks_create', {'id': 'owner-review', 'title': 'Review Beta launch', 'assignee_ids': [owner.principal_id],
                                            'story_ids': [beta_story['id']]})
        personal_call(beta, 'tasks_create', {'id': 'completed-review', 'title': 'Already reviewed', 'status': 'done', 'assignee_ids': [owner.principal_id]})
        personal_call(beta, 'tasks_create', {'id': 'unassigned-review', 'title': 'Not assigned to you'})
        personal_details = {'beta_business_id': other, 'beta_story_id': beta_story['id'], 'widget_story_ids': [story['id'] for story in widget_stories]}
    product_details = {}
    if products:
        # Agent proposals the owner reviews in Signoffs: a direct update and a
        # Product edit composed through context_review.
        def product_call(context, operation, arguments, revision=None, expect="ok"):
            result = runtime.application.invoke(context, operation, arguments,
                InvocationOptions(idempotency_key=new_id(), expected_revision=revision))
            if result.status != expect:
                raise RuntimeError('Product fixture operation failed: ' + str(result.to_dict()))
            return result
        product = product_call(owner, 'products_create', {'name': 'Widget Pro', 'attributes': {
            'one_liner': 'Current line', 'url': 'https://widget.example', 'description': 'Current description'},
            'platform_overrides': {'hackernews': {'name': 'Widget HN'}}}).data['product']
        task = product_call(owner, 'tasks_create', {'title': 'Product review task'}).data['task']
        target = {'type': 'product', 'id': product['id']}
        edge = product_call(owner, 'relationships_create', {'source': {'type': 'task', 'id': task['id']}, 'target': target,
            'role': 'concerns'}).data['relationship']
        agent_context = runtime.resolver.invocation_context(fixture.agent, business_id=fixture.fixture.business_id)
        direct = product_call(agent_context, 'products_update', {'product_id': product['id'], 'changes': {
            'attributes': {'one_liner': 'EXACT OWNER REVIEW COPY <img src=x onerror="window.__xss=2">', 'url': None,
                           'description': 'Long proposed description. ' * 30},
            'platform_overrides': {'reddit': {'one_liner': 'Reddit copy <b>bold</b>', 'url': None}}},
            'rationale': 'Sharper launch copy.'}, product['revision'], expect='signoff_required').signoff
        token = product_call(agent_context, 'context_reviews_list', {'item': target}).data['items'][0]['source_token']
        composed = product_call(agent_context, 'context_review', {'item': target, 'items': [{'relationship_id': edge['id'],
            'relationship_revision': edge['revision'], 'review_revision': 0, 'source_token': token, 'result': 'no_change',
            'explanation': 'Reviewed.'}], 'edit': {'operation': 'products_update', 'arguments': {'product_id': product['id'],
            'changes': {'attributes': {'description': 'REVIEW EDIT COPY'}}}}}, product['revision'], expect='signoff_required').signoff
        product_details = {'product_id': product['id'], 'product_signoff_id': direct['id'], 'product_review_signoff_id': composed['id']}
    inbox_details = {}
    if inbox:
        agent_context = runtime.resolver.invocation_context(fixture.agent)
        def inbox_call(context, operation, arguments, revision=None):
            result = runtime.application.invoke(context, operation, arguments,
                InvocationOptions(idempotency_key=new_id(), expected_revision=revision))
            if result.status != 'ok':
                raise RuntimeError('Inbox fixture operation failed: ' + str(result.to_dict()))
            return result.data
        story = inbox_call(owner, 'stories_create', {'title': 'Inbox browser Story', 'purpose': 'Verify exact source navigation'})['story']
        note = inbox_call(owner, 'notes_create', {'title': 'Inbox browser note', 'body': 'Initial note', **business_note})['document']
        for kind, item in [('stories', story), ('documents', note)]:
            inbox_call(agent_context, 'inbox_subscribe', {'source_type': kind, 'source_id': item['id']})
        # Owner comments use the private channel; the Global coordinator adds the shared one.
        manager_context = runtime.resolver.invocation_context(fixture.manager, business_id=fixture.fixture.business_id)
        inbox_call(manager_context, 'stories_note_add', {'story_id': story['id'], 'body': 'Coordinator update'})
        inbox_call(owner, 'notes_update', {'document_id': note['id'], 'body': 'Updated inbox note',
            'target_business_id': fixture.fixture.business_id}, note['revision'])
        claim(runtime.inbox, agent_context, new_id())
        coordinator = fixture.fixture.create('inbox-global', 'ordubashy')
        global_context = runtime.resolver.invocation_context(coordinator).select_business(None)
        global_document = fixture.fixture.document('inbox-global', 'Global source body for inbox browser', 'context/inbox-browser.md')
        inbox_call(global_context, 'inbox_subscribe', {'scope': 'global', 'source_type': 'documents', 'source_id': global_document['id']})
        claim(runtime.inbox, global_context, new_id())
        inbox_details = {'inbox_story_id': story['id'], 'inbox_note_id': note['id'],
            'inbox_global_agent_id': coordinator.id, 'inbox_global_document_id': global_document['id']}
    app = create_app(runtime, dashboard_root=dashboard_root or PATHS.component("dashboard"), origins=[origin], authentication=fixture.authentication,
        compatibility=DashboardCompatibility(runtime, background=runtime.background))
    if inbox:
        from test_global_agents import Broker
        from bazarlyk.transport.terminals import TerminalService
        app.state.http_api.terminals = TerminalService(runtime, Broker(), service_url=origin + '/mcp',
            version_provider=lambda: '2.1.263', preparation=lambda *args, **kwargs: None)
    return app, {"url": origin + "/?token=" + OWNER_TOKEN, "origin": origin, "business_slug": "widget",
        "business_id": fixture.fixture.business_id, "owner_id": fixture.fixture.owner.id, "agent_id": fixture.agent.id,
        "manager_id": fixture.manager.id, "signoff_id": pending.signoff["id"], "root": str(fixture.fixture.root),
        "platform_signoff_id": marketing.signoff["id"], "reddit_agent_id": reddit.id,
        "bulk_platform_signoffs_total": bulk_total + 1, "bulk_platform_signoffs_pending": bulk_pending + 1,
        "graph_note_id": graph_note["id"], "graph_person_id": person["id"], "graph_topic_id": launch["id"],
        "graph_note_path": graph_note["path"].removeprefix("context/"), **learning_details, **global_details, **table_details, **personal_details, **inbox_details, **product_details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--dashboard-root", type=Path, default=PATHS.component("dashboard"))
    parser.add_argument("--learnings", action="store_true")
    parser.add_argument("--global-signoffs", action="store_true")
    parser.add_argument("--personal-tasks", action="store_true")
    parser.add_argument("--tables", action="store_true")
    parser.add_argument("--global-tables", action="store_true")
    parser.add_argument("--inbox", action="store_true")
    parser.add_argument("--notes-only", action="store_true")
    parser.add_argument("--many-signoffs", action="store_true")
    parser.add_argument("--products", action="store_true")
    args = parser.parse_args()
    if args.port == 1049:
        parser.error("The live service port is forbidden for the isolated fixture")
    with tempfile.TemporaryDirectory(prefix="bazarlyk-browser-") as temporary, socket.socket() as listener:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        app, details = build_fixture(Path(temporary), origin, dashboard_root=args.dashboard_root, learnings=args.learnings, global_signoffs=args.global_signoffs, personal_tasks=args.personal_tasks, tables=args.tables, global_tables=args.global_tables, inbox=args.inbox, notes_only=args.notes_only, many_signoffs=args.many_signoffs, products=args.products)
        print(json.dumps(details), flush=True)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False, lifespan="on"))
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
