"""Host wiring for operational history; the snapshot engine itself is ``qorqut_operations.history_exports``.

``composition`` supplies the installation's paths, area resolution, clock and lifecycle to that engine, ``catalog`` the
cross-feature export coverage and ``projections`` its closed projections. Import nothing from here for the generic
classes: ``History``, ``HistoryObserver`` and the typed areas live in Operations.
"""
