"""Retained snapshot, recovery and maintenance helpers for existing owner data.

``snapshots`` is the active continuity and recovery format (storage and full recovery import it). ``managed_cutover`` moves an
installation's agents between runtimes and ``record_retention`` removes settled bookkeeping records; both are run on request, and
no service or startup path calls them. ``upgrade`` is the ordered offline schema upgrade: it holds the schema transforms in
release order and walks a store over every pending one, or back, in one transaction; a new transform is appended to its
``TRANSFORMS``. The service never converts a store when it opens one.
"""
