# Qorqbot

Qorqbot is an everyday assistant with agents, work records, shared knowledge,
CRM, calendar management and email guidance. It runs on your machine and keeps
its own installation state and work in a chosen folder.

The first distribution target is Ubuntu 24.04 on Intel/AMD 64-bit machines.
Users provide external tools and connect their own accounts. Start with the
[installation and requirements guide](qorqut-code/docs/distribution/README.md).
Windows uses [WSL2 with Ubuntu 24.04](qorqut-code/docs/distribution/WINDOWS.md).
[Native Apple silicon Mac preparation](qorqut-code/docs/distribution/MACOS.md)
is available; actual Mac build/launch acceptance is pending.

This repository contains the application and everyday connector sources:

| Directory | Contents |
| --- | --- |
| `qorqut-code/` | Host application, dashboard, Operations, Context, build and test tools |
| `qorqut-agentic/` | Managed agent instructions and integration |
| `qorqut-roles/agent-roles/` | Bundled role and skill definitions |
| `dependencies/` | First-party document-history and email-guidance packages |
| `qorqut-code/extensions/` | Google Calendar, Telegram and WhatsApp connectors |
| `extensions/qorqut-fastmail/` | Optional Fastmail/CalDAV connector |
| `services/email-engine/` | Optional email engine used with Fastmail |

Google Calendar is included without an active account connection. Fastmail,
Telegram and WhatsApp are separate optional installations. The WhatsApp adapter
and UI use Apache License 2.0; its separate native helper uses GPL version 3 only. Any helper binary download must include its complete
matching corresponding source. See the connector
[license scope](qorqut-code/extensions/whatsapp/LICENSE-SCOPE.md). Paper outreach
and content creation are outside this repository.

See [release artifacts](RELEASES.md), [contributing](CONTRIBUTING.md),
[security reporting](SECURITY.md) and the [license](LICENSE).

## License

First-party material is source-available under Business Source License 1.1 from
49 Agents LLC, with the Additional Use Grant in `LICENSE`. Each publicly released
version converts to Apache License 2.0 after two years. See
[license scope](LICENSE-SCOPE.md) for component-specific exceptions and third-party
terms. Source-available versions are not described as open-source releases before
that conversion.
