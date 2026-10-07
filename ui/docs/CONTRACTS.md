# Plan moved

[D55 local access](https://github.com/qorqutbot/qorqut-core/blob/main/docs/LOCAL-ACCESS.md)
retains only GET `/api/v1/session`, returning `{actor_id, api_version: 1}` for
workspace attribution. Browser authentication and POST/DELETE session are removed.

D53 adds the optional `ConversationItem.inbox_reference` read view, specified
in [INBOX-REFERENCE-UI.md](INBOX-REFERENCE-UI.md). It does not change immutable
entries, daily transcripts, pagination or operation names.

The authoritative [CONTRACTS.md](https://github.com/qorqutbot/qorqut-core/blob/main/docs/CONTRACTS.md) now lives in `qorqut-core`. The original planning baseline is preserved on `codex/chat-ui-plan` at `e7d2b56`.
