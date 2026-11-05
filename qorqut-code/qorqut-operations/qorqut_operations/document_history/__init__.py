"""Exact document history: shared content versions and business activity for registered documents and core records.

Current content stays in its registered file or domain table. The ``qorqut-business-docs-versioning`` library owns
content history and business activity facts; every write here goes through it on the caller's unit-of-work connection,
inside the same transaction as the committed source change, and nothing here opens a connection, commits or rolls back.

``text`` is the generic text and event mechanism, ``files`` the registered-file part (receipt capture, seeds and document
lifecycle events), ``lifecycle`` the document availability transitions around a file receipt, ``records`` the committed
record changes (documents, Tasks and Action facts), ``reads`` the owner-only readers and ``schema`` the library's tables.

The mechanism is generic: a host composes the concrete pieces it owns (a byte-capture callback per document kind, SQL
document projection descriptors and extra fact callbacks) and passes them in explicitly. A piece that is required and
absent is an error at composition, never a silent no-op.
"""
