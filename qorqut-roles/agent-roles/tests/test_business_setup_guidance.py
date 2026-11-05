"""Source-policy checks for the provider procedures and the retired business-mail route model.

The business setup checklist is retired (Projects transition) and, in v1.32, so are the Fastmail account and
business-email-setup role skills: email is routed to the Email Management package. Provider procedures live in
service-provisioning. The file keeps its name for continuity.
"""
from pathlib import Path
import re
import unittest

import os
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICE_OPS = ('cloudflare_zone_token_ensure', 'cloudflare_dns_records_ensure', 'environment_get')
RETIRED_MAIL_TEXT = re.compile(r"business-email-setup|fastmail-account|business_email_|signoffs_email_approve|account_email_"
                               r"|existing Fastmail alias model|\bemail_(?:send|reply|forward|conversations?_|thread_|decision_"
                               r"|assign|comment|capabilities|identities|mailboxes|folders?_)")
RETIRED_MAIL_SKILLS = ('business-email-setup', 'fastmail-account')
MAIL_ROUTED = ('skills/business-operations/SKILL.md', 'skills/service-provisioning/SKILL.md',
               'skills/project-creation/SKILL.md', 'skills/owner-focus/SKILL.md', 'skills/ecosystem-review/SKILL.md',
               'skills/context-relationships/SKILL.md', 'skills/operations-delegation/SKILL.md')
RETIRED_SETUP = ('business_setup_get', 'business_setup_update', 'business_setup_submit')
REUSABLE = ('skills', 'roles')


def manifest(role):
    return yaml.safe_load((ROOT / 'roles' / role / 'role.yaml').read_text())


def text(path):
    return ' '.join((ROOT / path).read_text().split())


class BusinessSetupGuidanceTest(unittest.TestCase):
    def test_ordubashy_brief_routes_email_to_email_management(self):
        role = manifest('ordubashy')
        brief = (ROOT / role['brief']).read_text()
        self.assertLessEqual(len(brief.strip()), 10000)
        flat = ' '.join(brief.split())
        self.assertIn('For all email work', flat)
        self.assertIn('consult Email Management first', flat)
        self.assertIn('it decides the account, sender, access method and send approval', flat)
        # The business-mail route model and automatic sends are gone from the default role.
        for stale in ('existing Fastmail alias model', 'business-email-setup', 'fastmail-account', 'business mail route',
                      'Version 3 sends are automatic', 'including new routes'):
            self.assertNotIn(stale, flat)
        # Provider work is explicit-request only and project creation is a direct procedure; no setup assignments remain.
        self.assertIn('Provider setup only when asked: service-provisioning.', flat)
        self.assertIn('Only you create a project (project-creation; no provisioning); the owner and workers ask you.', flat)
        self.assertNotRegex(flat, r'(?i)setup tasks|business-setup|setup checklist')

    def test_ordubashy_brief_keeps_its_existing_duties(self):
        flat = ' '.join((ROOT / manifest('ordubashy')['brief']).read_text().split())
        # Setup guidance must preserve the current journal and operational duties.
        for phrase in ('Journal review: owner-journal', 'Only when the owner asks, read the full visible entry',
                       'Return suggestions only; no filing, task/status changes, messages or journal edits',
                       'assign authorized browser work to a worker you granted browser access', 'operations-delegation', 'no native or main-session execution fallback',
                       'Ordubashy owns owner dialogue',
                       'Follow configuration and owner-focus',
                       'one direct owner action', 'use authoritative IDs, tasks and Inbox', 'Check duplicates',
                       'release condition and expiry', 'close tickets only after readback', 'Preserve context revisions',
                       'separate shared-instruction permissions', 'resolves the authorized recipient',
                       'Batch chores into one assignment', 'tasks_overview', 'System Dev', 'never self-approve',
                       'Parent Tasks follow ordinary Task completion and reopening permissions', 'urgent human Signoff'):
            self.assertIn(phrase.lower(), flat.lower())

    def test_provider_work_is_explicit_and_runs_directly(self):
        operations = text('skills/business-operations/SKILL.md')
        self.assertIn('Only Ordubashy creates a project, through project-creation; the owner and other roles ask Ordubashy', operations)
        self.assertNotIn('installation owner and Ordubashy create', operations)
        self.assertIn('creation provisions nothing', operations)
        self.assertIn('happens only on an explicit request under service-provisioning', operations)
        self.assertIn('as one operation with no worker per click or update', operations)
        self.assertNotIn('never through a worker', operations)
        description = yaml.safe_load(re.match(r'\A---\n(.*?)\n---\n',
            (ROOT / 'skills/service-provisioning/SKILL.md').read_text(), re.S)[1])['description']
        self.assertIn('only when the owner or a Task the owner authorized explicitly asks', description)
        self.assertIn('Creating a project never starts any of it', description)

    def test_new_skills_are_selected_only_by_ordubashy(self):
        procedures = ('project-creation', 'service-provisioning', 'calendar')
        for path in (ROOT / 'roles').glob('*/role.yaml'):
            role = yaml.safe_load(path.read_text())
            selected = {skill['id']: skill['path'] for skill in role.get('skills', [])}
            with self.subTest(role=role['id']):
                if role['id'] == 'ordubashy':
                    for skill in procedures:
                        self.assertEqual(selected[skill], f'skills/{skill}/SKILL.md')
                    self.assertNotIn('business-setup', selected)
                else:
                    for skill in (*procedures, 'business-setup'):
                        self.assertNotIn(skill, selected)
                        self.assertFalse({p for p in selected.values() if f'skills/{skill}/' in p})
                for skill in RETIRED_MAIL_SKILLS:
                    self.assertNotIn(skill, selected)

    def test_skill_frontmatter_names_match_manifest_ids(self):
        for skill in ('project-creation', 'service-provisioning', 'calendar'):
            body = (ROOT / 'skills' / skill / 'SKILL.md').read_text()
            meta = yaml.safe_load(re.match(r'\A---\n(.*?)\n---\n', body, re.S)[1])
            self.assertEqual(meta['name'], 'bazarlyk-role-' + skill)
            self.assertLessEqual(len(meta['description']), 600)
            self.assertNotIn('!`', body)

    def test_service_grants_are_scoped_to_ordubashy_and_the_setup_grants_are_retired(self):
        ordubashy = manifest('ordubashy')['default_tool_policy']
        for op in SERVICE_OPS:
            self.assertEqual(ordubashy[op], 'allow', op)
        # Creation stays with the existing grant; the checklist operations are gone from the active role.
        self.assertEqual(ordubashy['business_create'], 'allow')
        for op in RETIRED_SETUP:
            self.assertNotIn(op, ordubashy)
        for path in (ROOT / 'roles').glob('*/role.yaml'):
            role = yaml.safe_load(path.read_text())
            if role['id'] == 'ordubashy':
                continue
            with self.subTest(role=role['id']):
                policy = role['default_tool_policy']
                self.assertFalse({op for op in policy if op.startswith(('business_setup_', 'cloudflare_'))})
                self.assertNotIn('business_email_default_set', policy)
                self.assertNotIn('business_create', policy)
                self.assertNotIn('environment_get', policy)

    def test_mail_guidance_points_to_email_management_and_the_route_model_is_retired(self):
        for skill in RETIRED_MAIL_SKILLS:
            self.assertFalse((ROOT / 'skills' / skill).exists(), skill)
        for path in MAIL_ROUTED:
            with self.subTest(path=path):
                self.assertIn('Email Management', text(path))
        for path in sorted(ROOT.glob('roles/*/BRIEF.md')) + sorted(ROOT.glob('skills/**/*.md')):
            with self.subTest(retired=str(path.relative_to(ROOT))):
                self.assertIsNone(RETIRED_MAIL_TEXT.search(path.read_text()))
        operations = text('skills/business-operations/SKILL.md')
        for phrase in ('A Project owns no mailbox and grants no sender',
                       'use an alias-to-Project association only where the owner recorded it',
                       'Email Management is instructions only: it grants no tool and takes no mailbox list'):
            self.assertIn(phrase, operations)

    def test_reusable_guidance_has_no_installation_specific_facts(self):
        for folder in REUSABLE:
            for path in (ROOT / folder).rglob('*.md'):
                body = path.read_text()
                with self.subTest(path=str(path.relative_to(ROOT))):
                    for fact in ('.secrets', '/home/', 'cloudflare-token-provisioner.json',
                                 'no central zone-read credential today'):
                        self.assertNotIn(fact, body)

    def test_email_management_package_is_the_instruction_only_v3_when_present(self):
        # The package is a separate repository; check its contract when the sibling checkout is present.
        package = Path(os.environ.get('EMAIL_MANAGEMENT_PLUGIN', ROOT.parents[1] / 'dependencies/email-management'))
        if not (package / 'plugin.yaml').is_file():
            self.skipTest('email-management package checkout not present')
        self.assertEqual(yaml.safe_load((package / 'tools.yaml').read_text()), {'tools': {}})
        self.assertEqual(yaml.safe_load((package / 'rules.yaml').read_text()),
                         {'channel': 'email', 'approval': 'owner_input'})
        self.assertEqual(yaml.safe_load((package / 'plugin.yaml').read_text())['version'], '3.0.0')

    def test_verification_email_follows_the_mail_approval_rule(self):
        # A general provider request is not the owner's explicit waiver of send approval.
        provisioning = text('skills/service-provisioning/SKILL.md')
        resend = text('skills/service-provisioning/references/resend.md')
        self.assertIn("A verification email is outgoing mail: it follows Email Management's approval rule", provisioning)
        self.assertIn('which the provider request does not waive', provisioning)
        self.assertIn('reusing an existing saved approval that names this recipient and copy, or an explicit owner waiver', resend)
        for body in (provisioning, resend):
            self.assertNotIn('do not ask for another approval', body)
            self.assertNotIn('standing mail authority', body)
            self.assertNotIn('is part of that request', body)

    def test_service_provisioning_is_explicit_only_and_keeps_its_provider_procedures(self):
        guidance = text('skills/service-provisioning/SKILL.md')
        for phrase in ('only when the owner, or a Task the owner authorized, explicitly asks for provider work',
                       'Creating a project provisions none of it and there is no checklist that implies it',
                       'There are no owner-question forms or readiness protocol here',
                       'Never repeat a creation merely because a turn ended', 'omit `business_id` and `browser_key`',
                       "Never use the client's browser", 'This is the specified method, not a browser fallback',
                       'Do not create a worker per click', '`environment_set`',
                       'never wait in the browser while DNS propagates', 'RESEND_FROM',
                       "Application integration is development: the owner's unless the owner directly instructs a Global",
                       '`browser_turn` (lease-free)', 'read each value with `environment_get`',
                       'nothing is revoked automatically'):
            self.assertIn(phrase.lower(), guidance.lower())
        self.assertNotRegex(guidance, r'business_setup|business-setup|manage_business_setup|setup_id|founding_document_id')

    def test_provider_references_exist_and_are_linked(self):
        guidance = (ROOT / 'skills/service-provisioning/SKILL.md').read_text()
        folder = ROOT / 'skills/service-provisioning/references'
        for name in ('cloudflare', 'railway', 'resend', 'openrouter'):
            with self.subTest(reference=name):
                self.assertIn(f'(references/{name}.md)', guidance)
                self.assertTrue((folder / f'{name}.md').is_file())
        # The setup-only references are gone with the checklist.
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ['cloudflare.md', 'openrouter.md', 'railway.md', 'resend.md'])
        resend = text('skills/service-provisioning/references/resend.md')
        for phrase in ('A key present with the domain `not_started`', 'sending_access', 'copy control', 'do not create a duplicate',
                       'API acceptance alone is not delivery', 'Fastmail\'s inbound root MX',
                       'This is outgoing mail', 'Email Management\'s approval rule', 'recipient',
                       'The provider request is neither', '`environment_get`', 'one transactional',
                       'Never echo, print or log the key', '`message_id`, `status` and `observed_at`',
                       'dns_record_id, observed_at',
                       'domain, DNS and key work proceed before it'):
            self.assertIn(phrase, resend)
        cloudflare = text('skills/service-provisioning/references/cloudflare.md')
        for phrase in ('zone_discovery_unavailable', 'never create another issuer', 'recovery_required',
                       'com.cloudflare.api.account.zone.<zone ID>', 'are assertions', 'installation dependency',
                       'never ask the owner for a zone ID', 'Nothing is deleted or revoked automatically',
                       'verification_failed', 'replace_saved_token', '(`fastmail` or `resend`)',
                       'MX, TXT or CNAME only', '`_domainkey`', 'dns_record_id'):
            self.assertIn(phrase, cloudflare)
        self.assertNotIn('`other`', cloudflare)
        openrouter = text('skills/service-provisioning/references/openrouter.md')
        for phrase in ('optional', 'resetting monthly', '`OPENROUTER_API_KEY` before closing the dialog',
                       'never the key', 'no credit purchase', '"No expiration"', '`key_expiry: "never"`'):
            self.assertIn(phrase, openrouter)
        for name in ('cloudflare', 'railway', 'resend', 'openrouter'):
            self.assertNotRegex((folder / f'{name}.md').read_text(),
                                r'business_setup|business-setup|setup_id|setup\.protocol|owner-questions-and-context')

    def test_retired_founder_and_measurement_guidance_is_gone(self):
        self.assertFalse((ROOT / 'roles/founder').exists())
        for skill in ('business-founding', 'business-measurement', 'strategy-exploration', 'seo-autopilot'):
            self.assertFalse((ROOT / 'skills' / skill).exists(), skill)
        for path in sorted(ROOT.glob('roles/*/BRIEF.md')) + sorted(ROOT.glob('skills/**/*.md')):
            body = path.read_text()
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertNotIn('business_setup_submit', body)
                self.assertNotIn('Prepare business email', body)
                self.assertNotIn('business-measurement', body)

    def test_delegation_keeps_provider_browser_work_direct(self):
        delegation = text('skills/operations-delegation/SKILL.md')
        self.assertIn('Provider administration is explicit: for a provider service the owner asked for (service-provisioning)', delegation)
        self.assertIn('gains no software/design build authority', delegation)
        browser = text('skills/ordubashy-browser-control/SKILL.md')
        self.assertIn('owner-adopted request for exactly that step', browser)

    def test_application_handoff_routes_provider_gaps_to_ordubashy(self):
        operations = text('skills/business-operations/SKILL.md')
        for phrase in ('route it to Ordubashy', 'ask the owner to paste a token or add routine DNS',
                       'web-domain attachment', 'end-to-end login test'):
            self.assertIn(phrase, operations)


if __name__ == '__main__':
    unittest.main()
