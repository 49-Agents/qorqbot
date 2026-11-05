"""The host's neutral items: the host's records and rules beside the Operations item lookups (``qorqut_operations.records``),
and their composition into the engine's item resolver, relationship operations and context reviews.

``lookup`` (configured-global agents), ``resources`` (agent reach) and ``adapters`` (workflow roles, the Material rule for
historical email-message documents, direct-archive tables, Context navigation) are the host's adapters; the generic
algorithms are Operations'. Nothing here depends on a Task service. ``compose.build_neutral_items`` is called once by host
composition with the Application and the document service; the result is exposed independently by ``BusinessDomains`` and
handed to Tasks, documents, Focus and the inbox as consumers."""
