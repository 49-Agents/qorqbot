# Email Management: local instructions

This folder is the owner's own mail guidance for this Qorqut instance. It was copied once from the
Email Management package template. Later preparation never overwrites edits, and every agent that
holds Email Management reads the same files. It starts neutral: no accounts, addresses or
preferences are recorded until the owner states them.

## Read for email work

1. [`instructions.md`](instructions.md): the owner's preferences and the default rules.
2. [`inboxes/README.md`](inboxes/README.md) and the inbox files beside it: the accounts, addresses and
   aliases the owner uses.
3. [`providers/README.md`](providers/README.md): how mail is reached from this instance (a connector,
   the managed browser or a provider's own tools), plus any provider guide that has been added beside it.

Read what the request needs. A request about one account needs that account's inbox file and, when
one exists, its provider's guide.

## Doing the work

- Work out which account and sender the request means. Ask when more than one fits and the files
  do not decide.
- Use the access method recorded for that account. Confirm it is actually available in this session
  before relying on it.
- Reading, searching, drafting and organizing need no approval. Sending, replying and forwarding
  follow the approval rule in `instructions.md`.
- Handling an email creates no Task or note unless the owner's instructions ask for that.
- Say plainly what you observed. Never describe a draft or an unconfirmed send as delivered.

## Changing these instructions

When the owner states a new fact or preference about mail, write it into the file that fits:
a preference in `instructions.md`, an account or alias in a file under `inboxes/`, a way of reaching
a provider in `providers/`. Then tell the owner what changed and in which file. The change applies
the next time a file is read; no publication or restart is needed.

Edit only these local files. Do not put preferences in generated skill files, package source, chat
alone or Tasks. Never write a password, app password, API key, token or verification code here;
write where sign-in lives instead (for example "signed in in the managed browser").
