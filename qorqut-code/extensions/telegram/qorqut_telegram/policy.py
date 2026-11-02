from qorqut_operations.contracts import PolicyDecision
TELEGRAM_AGENT_OPERATIONS = frozenset({"telegram_send", "telegram_send_file", "telegram_history", "telegram_delivery_get"})
TELEGRAM_ROLES = frozenset({"ordubashy"})

def rule(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in TELEGRAM_AGENT_OPERATIONS:
        # The role is necessary, never sufficient: the operation also requires the exact configured agent for the chat.
        if role_id not in TELEGRAM_ROLES:
            return PolicyDecision("deny", "telegram_role")
        return PolicyDecision(overrides.get(name, defaults.get(name, "deny")),
                              "agent_override" if name in overrides else "role_default")
    if name == "telegram_notify":
        # Trusted registered workers only (they carry no role); an agent notifies through its own chat.
        return PolicyDecision(defaults.get(name, "deny") if role_id is None else "deny", "telegram_worker")
