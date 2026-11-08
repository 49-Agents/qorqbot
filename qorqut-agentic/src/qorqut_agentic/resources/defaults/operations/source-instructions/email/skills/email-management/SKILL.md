---
name: qorqut-plugin-email-management
description: Use for any email request - reading, searching, drafting, replying, forwarding, sending or organizing mail in Gmail or another mailbox; choosing which account, inbox or sender to use; and recording or changing the owner's saved mail instructions. Start from the owner's local mail instructions named in this skill.
---

# Email Management

The owner's mail instructions are local files, not this skill. They live here:

`{{EMAIL_MANAGEMENT_HOME}}`

Before any email work, read `{{EMAIL_MANAGEMENT_HOME}}/SKILL.md` and follow it. It points to
`instructions.md`, the files in `inboxes/` and `providers/README.md` (with any provider guide beside it);
read the ones the request needs. The owner may have edited them since you last looked, so read them again each
time instead of working from memory. The `instance-template/` folder next to this skill is only
the factory template: never treat it as the owner's instructions or edit it.

These hold even before you open the local files:

- Choose the account, inbox and sender explicitly. Use a Project association only where the owner
  recorded it in the local inbox files; never invent one or treat Project membership as mailbox
  ownership or permission. When that and the conversation do not settle the sender, ask the owner;
  never guess an identity.
- Use the access method the owner chose. A connector or tool exists only when this runtime shows
  it. If the chosen method is unavailable, say so and propose an alternative; do not switch silently.
- When an installed native sending tool supplies an editable exact-message Signoff whose approval
  queues the send, use that tool to create the Signoff. Approve queues the exact email automatically;
  Save only revises it. Do not use a generic question or send again after approval. This native tool
  contract takes precedence over older local instructions that say to send manually after an
  owner-input answer.
- Outgoing mail through other access methods (new, reply, forward) waits for the owner's saved approval unless the local
  instructions explicitly say otherwise. A draft, a queued action or an unconfirmed send is never
  reported as delivered.
- Record a new fact or preference in the local files, tell the owner what changed and where, and
  never put passwords, tokens or codes in them. Do not write it into generated skill files or
  package source.
