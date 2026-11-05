"""Qorqut Operations: the portable authority, storage and application engine.

A library with no authentication service, command entrypoints or import-time database initialization. It owns
two service endpoints (``SignoffService`` and ``BackgroundService``) whose worker threads start only when the host
explicitly invokes them. The trusted host composes the schema, catalog, policy rules and ports and owns every
transport."""

__version__ = "1.3.0.dev0"
