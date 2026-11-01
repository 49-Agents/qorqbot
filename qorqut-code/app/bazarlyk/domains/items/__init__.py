"""The host's neutral items: the retained domains' item lookups and relationship ports composed over the Operations
engine's item resolver, relationship operations and context reviews.

Nothing here depends on the Story service. ``compose.build_neutral_items`` is called once by host composition with the
Application, the document and email services and business setup (whose ``edge_guard`` is the protected-edge port); the
result is exposed independently by ``BusinessDomains`` and handed to Stories, Tasks, documents, Focus and the inbox as
consumers."""
