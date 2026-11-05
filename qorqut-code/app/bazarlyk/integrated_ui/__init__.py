"""Project-shared integrated UIs: agent-authored interfaces over typed, versioned host data.

The package contributes its storage (``schema``), its operation authority metadata (``catalog``) and the
``IntegratedUIService`` domain (``service``); the trusted host composition registers them and the API/transport
worker exposes the owner route and the MCP tools. Notifications to task followers are an injected callback owned by
``notifications`` (another slice); this package only guarantees the event, the deduplication key, the saved delivery
receipt and the transaction boundary.
"""