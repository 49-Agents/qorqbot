# qorqut-fastmail

Optional Fastmail provider for a Qorqbot host. The host core has no Fastmail code, tables, configuration or worker; everything
Fastmail-specific lives here and is switched on only by selecting the extension in the trusted installation configuration.

- `qorqut_fastmail.extension:create` is the factory selected through the host's `bazarlyk.extensions` mechanism.
- Public operations use the `fastmail_*` prefix and always name the Fastmail account (and, for composing, the sending identity).
  See [docs/OPERATIONS.md](docs/OPERATIONS.md) for the operation, grant and configuration contract.
- The extension supplies the `fastmail` CalDAV connection type to the host's generic `calendar_*` operations. Google Calendar
  is supplied by its own bundled extension.
- For Paper's explicit optional sender route it exposes a narrow `route_client()` interface (`route_change`, `routes`).
- Mail records of the retired per-Project mailbox model are not part of a fresh install. An offline, read-only export tool and
  the transition notes live in [docs/LEGACY-MAIL.md](docs/LEGACY-MAIL.md).

## Email Management provider guide

The core Email Management package is provider-neutral: it guides email work through whatever connector or browser is available and
works with no provider guide installed. The Fastmail-specific guidance (access, aliases and DNS, the native Signoff send contract,
receipts and no-resend rules) ships here as package data:

```
qorqut_fastmail/resources/email-management/providers/fastmail.md
```

Selecting this extension imports its guide into the managed Agentic package `imported-fastmail-metadata`. The host injects
that authored guide once when an agent can use a Fastmail operation. The guide supplements the core Email Management instructions
and the account's `inboxes/` file without replacing its sender choice or approval setting. Normal managed supplier updates retain
local edits and refuse conflicts; no file is copied over an existing provider instruction.

The selected extension also owns the mandatory sending policy, local delivery receipts and exact-content approval renderer.
Without it selected, the host advertises no Fastmail operations and loads neither the provider guide nor its browser asset.

## What it does not do

It holds no provider credential and never talks to Fastmail directly: all mail traffic goes through the separate email engine's
account API with the engine administration token. It owns no database table and adds nothing to the host's schema or history. No
mailbox, Project, folder or alias belongs to its configuration; aliases are ordinary identities of the one account.

## Selecting it

```json
{"factory": "qorqut_fastmail.extension:create",
 "config": {"engine_url": "http://127.0.0.1:8787", "admin_token_file": "/absolute/private/engine-admin-token"}}
```

The package must be installed in the host's environment; the host core does not depend on it. The host's preparation tool
(`qorqut-code/tools/prepare-local.py --with-fastmail`) builds and installs it there, installs `services/email-engine` into the
separate `email-engine-venv`, and applies this package's own `constraints.txt` (its calendar parser pin) to the host environment.
A standard preparation (no `--with-fastmail`) builds none of these and records no Fastmail selection. Preparation configures and
starts nothing: running the engine and naming the extension remain a deliberate installation step.

## Checks

```
pip install -e '.[test]'
python -m pytest -q tests
```

The tests run an in-process fake engine on disposable data. They never open a network connection, credential, mail account or
operational database. The host-composed cases import the host's own test helpers from the sibling `qorqut-code/app/tests`
(override with `QORQUT_HOST_TESTS`).
