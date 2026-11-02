"""The host's neutral items: the retained domains' item lookups and relationship ports composed over the Operations
engine's item resolver, relationship operations and context reviews.

Nothing here depends on a Task service. ``compose.build_neutral_items`` is called once by host composition with the
Application and the document service; the
result is exposed independently by ``BusinessDomains`` and handed to Tasks, documents, Focus and the inbox as
consumers."""
