# Telegram extension

This separately installed connector adds owner Telegram messaging, controlled file/photo sends, reply history, Signoff notifications and owner reminder delivery. Qorqbot runs without it; reminders remain recorded and visible while no owner delivery transport is configured.

The Python distribution is `qorqut-telegram`. It includes its Pillow dependency separately from the default application. Build from this directory with `python -m pip wheel --no-deps .`. Install the wheel and its dependencies into the same environment as the matching Qorqbot host, then select the trusted factory in the private installation configuration:

```json
{"factory": "qorqut_telegram.extension:create", "config": {}}
```

Selection contributes nine transport tables and changes the instance schema fingerprint. A fresh instance must be initialized with this selection. Existing instances require the supported offline extension-storage transition before changing selection; simply editing a running installation's extension list is refused. Keep a verified private backup, and never discard old transport receipts or pending sends. Extension download hosting is not selected yet.

The private bridge configuration is `instance/config/telegram.json`. Runtime files, secret spools, sealed attachments, locks and gateway state are under `instance/extensions/telegram/`. Configuration pins the installation, owner, bot and destinations; it supports the coordinator chat, Signoffs and Urgent Notifications. No accounts, chat identifiers or credentials are supplied by the package. The example command prints placeholders only:

```sh
python -m qorqut_telegram example
python -m qorqut_telegram validate --config /chosen/qorqbot/instance/config/telegram.json
```

Keep the configuration and token file private. Set the token path inside the chosen installation folder. Selecting the connector starts no communication until its private configuration is explicitly enabled. Agent tools additionally require the current bound coordinator and an explicit existing permission grant; an override cannot grant another role access. Unknown delivery outcomes are retained for reconciliation and never automatically resent as successful.

Agentic supplies the guidance at the stable native alias `qorqut_telegram/resources/telegram-replies.md` only when this extension is selected. The historical optional role instructions are retained in Agentic’s source library under `telegram/qorqut_telegram/optional/roles/`; grant only the desired operations through the existing agent permission mechanism. No extra authorization system is introduced.

Owner HTTP operations use `/api/root/extensions/telegram/<operation>`, protected by the host's existing owner/origin checks. The connector CLI uses those routes. Native transcript history retains its historical Telegram input type independently of the connector. Private source reads require the selected connector and its current authorization checks; disabling it never makes archived private content public.

Tests use synthetic databases, bot responses, media bytes and native sessions. Source tests are not included in runtime wheels. No live provider credentials are needed for the focused composition, channel and retention checks.
