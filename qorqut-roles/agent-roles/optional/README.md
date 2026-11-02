# Optional connector instructions

Instruction material for connectors that are installed separately. **Nothing here is selected by
any default role manifest, and nothing here is bundled with the default catalog.** The default
roles in `roles/` load and work without this directory.

Each connector directory holds its existing guidance, moved here unchanged from the default
skills and briefs, and a `CONNECTOR.md` listing what the connector's own package must supply when
it is installed: the skill files, any brief section, and the role grants for operations that only
exist while the connector is installed. A role manifest may grant only operations the host has
registered, so these grants never belong in a default `role.yaml`.

| Connector | Material | Needs from its package |
|---|---|---|
| `telegram/` | Owner-communication skill and the brief section that points to it | The four `telegram_*` operations and their Ordubashy grants |
| `google-calendar/` | Connection setup procedure and Google calendar limits | The Google calendar provider and its setup helper |
| `fastmail/` | Fastmail calendar availability note | The Fastmail package's calendar adapter |

Paper and content-creation guidance is not part of this repository.
