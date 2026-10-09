# First-message Chat acceptance

2026-10-09. Manager accepted `f8f6fc6dc51edeb1459b602c3db4835001cc5a17`
(ACLA completion 347, approval 348). The clean branch matched its remote SHA.
Both production changes were reviewed. Four focused cold-discovery, freshness,
single-event consumption and uncertain-input checks passed independently in
5.839 seconds; the builder's 38 owning checks also passed.

Original pre-Chat evidence contains no hook, message, attempt or native file.
The first actual Codex notification used startup event 11; the second used
normal Stop event 45. Each had one submitted attempt and an agent-attributed
acknowledgment. The source and host raw copies matched all 89,100 bytes; six
dated transcript entries matched HTTP content and provenance. Cleanup records
show no remaining owned processes or namespace.

The [actual flow report](first-chat-r1.md) preserves the ordinary native MCP
permission waits and their single-call approvals. Responses came from the local
canned provider: this accepts CLI/tool/capture integration without a manual
seed, not general model behavior. Public input_ready remains conservative;
parser coverage, notification presentation, browser/VM and broader capture
gates remain separate. Private checks are `manager-review347-tests.log` and
`manager-review347-evidence.json` in the run state directory.
