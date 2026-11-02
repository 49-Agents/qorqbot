# Business Settings writes

`environment_set(key, value)` writes one binding in the selected business. It is
available through MCP and the application API under ordinary role permissions.
Ordubashy 1.22.2 grants `allow`; other roles require an explicit grant. Global
Settings and runtime identity bindings are outside this operation.

Values are ordinary tool arguments, including credentials. Writes preserve
unrelated keys and comments, and do not copy inherited Global values. An empty
value creates an empty business override. Existing role-private bindings retain
their allowed roles and require that the caller already belongs to an allowed
role. New bindings use the business's normal Settings store.

The standard application pipeline applies hash-checked file changes with durable
receipts, idempotency and optional exact-change signoff. The response contains
only the key, configured status and business origin. Settings reads see the new
value immediately. Existing terminal processes keep their environment until a
new session is started; this operation does not restart agents.

Deploy core, publish/adopt Ordubashy 1.22.2, and refresh its prepared client
configuration. No schema migration is required. This does not create provider
tokens or provision external services; it enables the agent to store their results.

## Scoped configuration

Ordubashy 1.23.0 added `business_config_patch` within its existing business scope. The settings entry point checks current owner or scoped Ordubashy
identity. Other roles do not gain settings administration through an allow
extension alone. General `business_config_update` keeps its existing permissions.

`business_config_patch(changes)` uses the current `expected_revision` and a stable
`idempotency_key`. Objects merge recursively, arrays/scalars replace, and null
removes ordinary keys. Existing engineering validation still applies, and a `browser` section cannot be set;
registered identity, ownership and storage paths cannot change. Clear engineering
lists explicitly; omitting a protected block preserves it. Use the existing task,
record and environment tools for their respective settings.

The former `business_email_get/update/sync` mailbox administration is retired from core (see [Email ownership](EMAIL-OWNERSHIP.md)).
