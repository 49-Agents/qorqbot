from qorqut_operations.application.registry import CatalogEntry

ENTRIES = (
    CatalogEntry("telegram_send", False, "records", frozenset({"mcp"})),
    CatalogEntry("telegram_send_file", False, "records", frozenset({"mcp"})),
    CatalogEntry("telegram_history", False, "read", frozenset({"mcp"})),
    CatalogEntry("telegram_delivery_get", False, "read", frozenset({"mcp", "dashboard"})),
    CatalogEntry("telegram_notify", False, "records", frozenset({"dashboard", "worker"})),
    CatalogEntry("telegram_status", False, "read", frozenset({"dashboard"}), True),
    CatalogEntry("telegram_delivery_resolve", False, "records", frozenset({"dashboard"}), True),
)
