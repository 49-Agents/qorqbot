"""The coordinator holds the owner's reach, and what it is refused is a written list.

The owner works through Ordubashy. A tool it cannot use, or must have approved first, has to be on one of the lists below.
A new operation that Ordubashy would be refused fails this test until someone decides, in writing, that it stays the
owner's. Nothing is withheld by omission from a role file any more.
"""
from importlib import resources
import unittest

import yaml

from qorqut.composition.catalog import builtin_registry
from qorqut.composition.policy import ORDUBASHY_WITHHELD, resolve_operation_policy

# Only the installation owner decides these. Each is refused to every agent before any rule runs.
OWNER_ONLY = frozenset({
    # Approving, reviewing and retrying a proposal: the coordinator cannot approve its own work.
    'signoffs_review', 'signoffs_retry_apply',
    # Who the coordinator is.
    'management_coordinator_set',
    # The owner's private conversations and side chats.
    'owner_conversations_list', 'owner_conversation_peers', 'owner_conversation_related', 'owner_conversation_mark_read',
    'side_chats_create', 'side_chats_rename', 'side_chats_close',
    # The Learning Applier's review and the owner's own Context filing controls.
    'learnings_review', 'learnings_edit', 'learnings_intake_status',
    'context_changes', 'context_filings', 'context_retract', 'context_upload_map', 'journal_upload_to_filer',
    # Archived projects and document history: owner views today.
    'project_archived_list', 'project_restore', 'document_history_get', 'project_history_list',
})
# The owner approves each of these: the coordinator proposes, a Signoff decides.
NEEDS_APPROVAL = frozenset({
    'project_archive', 'tables_archive', 'owner_action_propose', 'roles_publish', 'plugins_publish',
    'shared_instructions_update', 'learnings_extract',
})
# The role file denies direct Context writes by name; the coordinator files through context_instruct and context_edit.
ROLE_DENIES = frozenset({
    'context_document_update', 'context_notes_create', 'context_notes_move', 'context_topics_create', 'context_topics_move',
    'context_people_create', 'context_people_update', 'context_people_merge', 'context_people_affiliation_set',
})
# The Learning Applier's own tools, decided by their own rules.
LEARNING_APPLIER_ONLY = frozenset({'learnings_decide', 'context_questions', 'context_question_edit'})


def role_defaults(registry):
    text = resources.files('qorqut_agentic').joinpath(
        'resources/defaults/roles/imported-roles/native/roles/ordubashy/role.yaml').read_text()
    # A name contributed by a part this registry does not compose is not judged here.
    return {name: mode for name, mode in yaml.safe_load(text)['default_tool_policy'].items() if registry.known(name)}


class OrdubashyOwnerParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = builtin_registry()
        cls.defaults = role_defaults(cls.registry)

    def decision(self, name, scope, *, role='ordubashy', overrides=None):
        return resolve_operation_policy(name, self.defaults, overrides or {}, self.registry, role_id=role, scope=scope)

    def refused(self):
        found = {}
        for entry in self.registry.entries():
            for scope in ('global', 'project'):
                decision = self.decision(entry.name, scope)
                if decision.mode != 'allow':
                    found.setdefault(entry.name, set()).add(decision.mode)
        return found

    def test_everything_the_coordinator_is_refused_is_on_a_written_list(self):
        refused = self.refused()
        not_agent_tools = {entry.name for entry in self.registry.entries()
                           if 'mcp' not in entry.exposure and not entry.owner_only}
        written = OWNER_ONLY | NEEDS_APPROVAL | ROLE_DENIES | LEARNING_APPLIER_ONLY | ORDUBASHY_WITHHELD
        unexplained = sorted(set(refused) - written - not_agent_tools)
        self.assertEqual(unexplained, [], 'Ordubashy is refused these without a written decision: allow them, or add '
                                          'them to the list they belong to')
        # A list that names a tool Ordubashy can in fact use, or one that no longer exists, has gone stale.
        self.assertEqual(sorted(written - set(refused)), [])

    def test_the_lists_say_why(self):
        refused = self.refused()
        owner_only = {entry.name for entry in self.registry.entries() if entry.owner_only}
        self.assertEqual(owner_only, OWNER_ONLY)
        self.assertEqual({name for name, modes in refused.items() if 'signoff' in modes}, NEEDS_APPROVAL)
        for name in ROLE_DENIES:
            self.assertEqual(self.defaults[name], 'deny')
        for name in ORDUBASHY_WITHHELD:
            self.assertEqual(self.decision(name, 'global').provenance, 'fallback')

    def test_a_tool_no_role_names_reaches_the_coordinator_and_nobody_else(self):
        self.assertNotIn('tags_create', self.defaults)
        allowed = self.decision('tags_create', 'global')
        self.assertEqual((allowed.mode, allowed.provenance), ('allow', 'ordubashy_owner_parity'))
        for role in ('global-worker', 'learning-applier', None):
            self.assertEqual(self.decision('tags_create', 'global', role=role).mode, 'deny')

    def test_an_explicit_deny_still_wins(self):
        self.assertEqual(self.decision('tags_create', 'global', overrides={'tags_create': 'deny'}).mode, 'deny')

    def test_routines_need_no_approval(self):
        for name in ('routines_create', 'routines_update', 'routines_delete', 'routines_resume', 'routines_run'):
            self.assertEqual(self.decision(name, 'global').mode, 'allow')


if __name__ == '__main__':
    unittest.main()
