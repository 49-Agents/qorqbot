from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ROLES = ('ordubashy', 'learning-applier')
GLOBAL_ROLES = ACTIVE_ROLES
# The repeatable worker role: it holds no dedicated owner channel, Context writes or administration by default.
WORKER_ROLE = 'global-worker'
# Retired roles cannot be rebuilt or republished from this source; their published
# artifacts, records and Git history are kept elsewhere.
RETIRED_ROLES = ('onbashy', 'strategist', 'marketing', 'outreach-specialist', 'business-agent', 'dev', 'coder',
                 'founder', 'global-marketing')
RETIRED_SKILLS = ('staffing', 'browser-control', 'business-triage', 'campaign-execution', 'business-code',
                  'business-engineering', 'dev-ui-qa', 'ui-qa-delegation', 'business-founding', 'business-core',
                  'business-planning', 'business-products', 'business-measurement', 'content-creation',
                  'customer-approach', 'daily-report', 'marketing-plan', 'name-choosing', 'personal-context',
                  'pull-strategy', 'seo-autopilot', 'strategy-exploration', 'stripe-management', 'stripe-read',
                  'business-testing')
# The new host catalog has no such operations, so no role may allow or deny them.
REMOVED_CONTEXT_OPERATIONS = ('context_summarize', 'context_map_generate')


def load(role):
    return yaml.safe_load((ROOT / 'roles' / role / 'role.yaml').read_text())


class ContextAuthorityTest(unittest.TestCase):
    def test_owner_assistant_creates_and_updates_context_without_routine_signoff(self):
        for role in ('ordubashy',):
            manifest = load(role)
            policy = manifest['default_tool_policy']
            self.assertEqual(policy['business_archive'], 'signoff')
            for op in ('context_upload', 'context_retry', 'notes_create', 'notes_update'):
                with self.subTest(role=role, operation=op):
                    self.assertEqual(policy[op], 'allow')
            for op in REMOVED_CONTEXT_OPERATIONS:
                with self.subTest(role=role, removed=op):
                    self.assertNotIn(op, policy)
            self.assertNotEqual(policy['context_people_merge'], 'allow')
            self.assertNotEqual(policy['shared_instructions_update'], 'allow')
            brief = ' '.join((ROOT / manifest['brief']).read_text().split())
            self.assertLessEqual(len(brief), 10000)
            self.assertIn('perform permitted operations', brief)

    def test_removed_context_operations_are_named_by_no_role(self):
        for path in sorted((ROOT / 'roles').glob('*/role.yaml')):
            policy = yaml.safe_load(path.read_text())['default_tool_policy']
            for op in REMOVED_CONTEXT_OPERATIONS:
                with self.subTest(role=path.parent.name, removed=op):
                    self.assertNotIn(op, policy)

    def test_scoped_context_corrections_and_uploads(self):
        # Both globals submit and edit the one shared Context through the existing operations.
        for name in ACTIVE_ROLES:
            policy = load(name)['default_tool_policy']
            with self.subTest(role=name):
                self.assertEqual(policy['context_instruct'], 'allow')
                self.assertEqual(policy['context_edit'], 'allow')
                self.assertEqual(policy['context_upload'], 'allow')
                self.assertNotEqual(policy.get('context_people_merge'), 'allow')
        for role in ('ordubashy', 'learning-applier'):
            self.assertEqual(load(role)['scope_kinds'], ['ecosystem'])

    def test_no_role_writes_the_legacy_context_tree(self):
        # 2026-09-21: the context base is the filer and the agents' context; the file tree under
        # hq/context is read by the dashboard only. People, topics and notes enter the base through
        # uploads (and owner instructions), never through these file-writing operations.
        for role in ACTIVE_ROLES:
            policy = load(role)['default_tool_policy']
            for op in ('context_document_update', 'context_topics_create', 'context_topics_move',
                       'context_notes_create', 'context_notes_move', 'context_people_create',
                       'context_people_update', 'context_people_affiliation_set', 'context_people_merge'):
                with self.subTest(role=role, operation=op):
                    self.assertNotEqual(policy.get(op, 'deny'), 'allow')
                    self.assertNotEqual(policy.get(op, 'deny'), 'signoff')

    def test_business_notes_stay_with_the_coordinator_and_the_context_affiliation_stays_closed(self):
        policy = load('ordubashy')['default_tool_policy']
        for op in ('notes_list', 'notes_get', 'notes_create', 'notes_update'):
            self.assertEqual(policy[op], 'allow', op)
        self.assertNotEqual(load('learning-applier')['default_tool_policy'].get('context_people_affiliation_set'), 'allow')

    def test_globals_share_the_owner_default_development_rule(self):
        # Businesses have no resident Dev: development is the owner's by default, his direct
        # instruction is carried out by the instructed global, and engine/dashboard code stays protected.
        for role in GLOBAL_ROLES:
            brief = ' '.join((ROOT / load(role)['brief']).read_text().split())
            with self.subTest(role=role):
                self.assertIn("is the owner's by default", brief)
                self.assertIn('never edit qorqut engine or dashboard code', brief.lower())
                self.assertIn('directly instruct' if role == 'learning-applier' else 'direct instruction', brief)
                self.assertNotIn('Dev builds', brief)
                self.assertNotIn('ui-qa-delegation', brief)
                self.assertIn('landing pages and site/UI design) is the owner\'s by default', brief)
                self.assertNotIn('visual assets', brief)
                # Who makes marketing creative belongs to the content extension, not to core roles.
                self.assertNotIn('arketing creative', brief)

    def test_retired_role_sources_and_skills_are_removed(self):
        # Only the three general roles are publishable from this source.
        self.assertEqual(sorted(path.parent.name for path in (ROOT / 'roles').glob('*/role.yaml')), sorted([*ACTIVE_ROLES, WORKER_ROLE]))
        self.assertEqual(sorted(path.name for path in (ROOT / 'roles').iterdir()), sorted([*ACTIVE_ROLES, WORKER_ROLE]))
        for role in RETIRED_ROLES:
            self.assertFalse((ROOT / 'roles' / role).exists(), role)
        for skill in RETIRED_SKILLS:
            self.assertFalse((ROOT / 'skills' / skill).exists(), skill)
        selected = set()
        for path in (ROOT / 'roles').glob('*/role.yaml'):
            manifest = yaml.safe_load(path.read_text())
            with self.subTest(role=manifest['id']):
                self.assertNotIn(manifest['id'], RETIRED_ROLES)
                self.assertEqual(manifest['scope_kinds'], ['ecosystem'])
                self.assertTrue((ROOT / manifest['brief']).is_file())
                for skill in manifest.get('skills', []):
                    self.assertNotIn(skill['id'], set(RETIRED_SKILLS) - {'browser-control'})
                    self.assertTrue((ROOT / skill['path']).is_file(), skill['path'])
                    selected.add((ROOT / skill['path']).parent)
        # Every packaged skill directory is selected by a surviving role.
        self.assertEqual({path.parent for path in (ROOT / 'skills').glob('*/SKILL.md')}, selected)

    def test_active_text_names_no_retired_role(self):
        for path in (ROOT / 'roles').glob('*/role.yaml'):
            manifest = yaml.safe_load(path.read_text())
            files = [ROOT / manifest['brief']]
            for skill in manifest.get('skills', []):
                files.extend((ROOT / skill['path']).parent.rglob('*.md'))
            for file in files:
                text = file.read_text()
                for name in ('Onbashy', 'Strategist', 'Founder', 'Global Marketing', 'Outreach Specialist',
                             'outreach-specialist', 'Marketing agent', 'staffing'):
                    with self.subTest(role=manifest['id'], file=str(file.relative_to(ROOT)), name=name):
                        self.assertNotIn(name, text)
