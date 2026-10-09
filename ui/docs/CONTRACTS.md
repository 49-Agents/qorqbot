D59 adds optional nullable RuntimeView.attention, as specified by the
[frozen native-attention contract](NATIVE-ATTENTION.md). Older core responses
remain supported. Native prompt detection stays in core.

D57 adds the validated browser observation/request views and nullable
ConversationItem.browser_request annotation. See [UI implementation](BROWSER-IMPLEMENTATION.md)
and the [frozen core contract](https://github.com/49-Agents/qorqbot/blob/fabb57151bb7f842a0e01c02b031798bf7081160/core/docs/BROWSER-APP-INTEGRATION.md).
The UI uses the existing operation envelope and core-only browser stream route.

# Plan moved

[D55 local access](../../core/docs/LOCAL-ACCESS.md)
retains only GET `/api/v1/session`, returning `{actor_id, api_version: 1}` for
workspace attribution. Browser authentication and POST/DELETE session are removed.

D53 adds the optional `ConversationItem.inbox_reference` read view, specified
in [INBOX-REFERENCE-UI.md](INBOX-REFERENCE-UI.md). It does not change immutable
entries, daily transcripts, pagination or operation names.

The authoritative [CONTRACTS.md](../../core/docs/CONTRACTS.md) now lives in `qorqut-core`. The original planning baseline is preserved on `codex/chat-ui-plan` at `e7d2b56`.
