"""Operational source documents.

``serialization`` is the frontmatter framing of a registered document, ``store`` the registered-file store (scopes, bytes,
hash, path, attribution, archive), ``library`` the source selection, parsing, cache, search and cursors, ``operations`` the
source read and mutation operations, ``journal`` the owner journal reads and ``schema`` the ``documents`` table. The helper
modules (``materials``, ``evidence``, ``people``, ``business_notes``, ``relationships``, ``note_snapshot``,
``journal_access``) hold source classification, attribution and journal visibility.

Submodules import what they use; nothing is re-exported here, so importing one source module never loads the others."""
