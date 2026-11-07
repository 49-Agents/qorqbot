# Email Management

Instruction-only package for all of an owner's email work. Version 3.x replaces the 2.x
toolkit manifest: it declares no tool, registers no mailbox and holds no credential. It
points an agent to the owner's own editable mail instructions, which say which accounts and
aliases exist, how each is reached (a model connector, the managed browser, or the native tools
of an optional provider package), which sender to use, how outgoing mail is approved, and whether
handled mail is recorded as Tasks. Core works through whatever connector or browser is available;
Qorqut has no mail-provider backend, and a Gmail account uses a connector or the browser. A provider
that needs its own setup and tool guidance ships that as a separate guide with its own optional
package (for example the optional Fastmail package); core works when no such guide is installed.

Publishing or granting this package gives no tool or capability. The default coordinator agent
receives it by default; other agents hold it only when granted explicitly.

## Package

The canonical package sources now live in Agentic's
`resources/defaults/operations/source-instructions/email` library. The names
below are relative to that library. Native package IDs and installed catalog
locations are unchanged; this directory retains licensing and package history.

- `plugin.yaml`: id `email-management`, version `3.2.0`, schema version 1, `scope_kinds`
  `[ecosystem]`. The scope kind says where the package may be granted and grants nothing.
- `tools.yaml`: `tools: {}`.
- `rules.yaml`: `channel: email`, `approval: owner_input`. This is the guidance policy for access
  methods without a native sending tool: sends are approved by an owner-input request (see below).
  An installed native sending tool with its own editable Signoff is governed by that tool, and the
  serialized rule does not change.
- `schemas/config.schema.json`: the configuration is an empty object. No mailbox, Project,
  provider token or single-account setting is required or accepted. The default is `{}`.
- `skills/email-management/`: the declared skill subtree.
  - `SKILL.md`: a short discovery skill whose literal `{{EMAIL_MANAGEMENT_HOME}}` is
    resolved by preparation to the instance-owned directory below.
  - `instance-template/`: the neutral template for that directory (`SKILL.md`,
    `instructions.md`, `inboxes/README.md`, `providers/README.md`).

`providers/README.md` is the only provider file in the template. It describes the generic routes
(connector, managed browser, an optional provider's tools), how to record the chosen route in an
inbox file and how to find a provider guide that has been added next to it. Provider-specific
guides are not part of this package; the optional Fastmail package packages its own.

## Local instructions

Installation imports the factory template, or an explicitly supplied offline authored guide,
into the managed Agentic layer at `plugins/imported-email-management/guidance/email/`.
Native preparation resolves `{{EMAIL_MANAGEMENT_HOME}}` to that canonical guide and never
seeds or overwrites another copy. Agents with this package use the same canonical guide;
it is not per Project.
The shipped templates contain no accounts, addresses, Project mappings or credentials. Their
examples are labelled, synthetic and inactive. Provider-guide adoption by an optional installer
is not implemented yet. A later installer must preserve authored files and use the supported
managed-guide authoring/import path; a packaged provider resource is not automatically active.

When the owner states a mail fact or preference, the agent writes it into those local files
and tells the owner what changed. Edits take effect the next time the files are read; they
need no package publication, generated-skill edit or restart. The generated native skill is
only the entry point and is not the editable source.

## Approval

Outgoing mail (new, reply, forward) waits for an owner decision by default. There are two mechanisms:

- **Native sending tool with its own Signoff.** When an installed tool supplies an editable
  exact-message Signoff whose approval queues the send, the agent uses that tool. Save only revises
  the message, Approve queues exactly that message, and the agent neither creates a second generic
  approval nor sends again afterward. The tool requires its Signoff itself, so a prose waiver does
  not bypass it. This contract takes precedence over older local instructions that say to send by
  hand after an owner-input answer.
- **Every other method** (connector, browser). The agent creates an input request with
  `signoffs_input_create`, reads the saved response with `signoffs_get`, and sends only after an
  affirmative one. The request executes nothing, and the instruction is carried out by the agent
  rather than enforced by a browser or connector gate. An owner may explicitly turn the default off
  in the local instructions for these methods; that is a saved instruction, not a policy service.

Neither mechanism broadens sending authority, and the package adds no permission framework.

## Upgrading from 2.x

Version 3.0.0 removes every `email_*` tool declaration, the `mailboxes` configuration and the
project/account scope and alias-routing guidance. A 2.x grant that stored
`{"mailboxes": [...]}` does not validate against the empty config schema; re-pin the grant
with `{}`. Republish the package, then re-prepare the agents that hold it. Nothing is deleted
from any provider, and existing local instruction files are kept.

## Version 3.2.0

The factory template no longer contains `providers/gmail.md` or `providers/fastmail.md`; it contains
one neutral `providers/README.md`. Provider-specific Fastmail setup, alias, DNS and native-Signoff
guidance moved to the optional Fastmail package, and the generic connector/browser guidance moved
into the neutral file. Existing 3.0 and 3.1 packages remain importable with their original template
shape. Re-preparation preserves the active canonical guide, including authored provider files.
A later supplier import still uses the supported update plan and its conflict checks. The package
id, empty configuration, tool-less contract and `{{EMAIL_MANAGEMENT_HOME}}` placeholder are unchanged.
