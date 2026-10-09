# D59 — Native approvals and questions in Chat

Implement the Manager's [frozen core contract](https://github.com/qorqutbot/qorqut-core/blob/codex/native-attention-plan/docs/NATIVE-ATTENTION.md).
RuntimeView gains optional nullable attention. The UI renders the current
prompt above the composer, labels agents needing input and opens their existing
Terminal. Core alone recognizes native prompts and owns freshness/identity.

Keep old-core compatibility, escaped plain text, one current notice, draft and
scroll preservation, and honest stale/unknown states. This is desktop-only.
The existing tests can cover presentation; do not represent synthetic fixtures
as the separately requested hands-on Codex Chrome session.
