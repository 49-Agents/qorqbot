"""Retained snapshot/recovery helpers and explicit conversions for existing owner data.

``snapshots`` is the active continuity and recovery format (storage and sandbox recovery import it). ``journal_notes``,
``project_note_tags`` and ``crm_upgrade`` are stand-alone converters that are only run on request; no service or startup path
calls them. ``crm_upgrade`` is the one schema upgrade: an offline copy of a stopped pre-CRM store into a new CRM-capable one,
recognized by the frozen ``crm_baseline_fingerprint.json``. The service never converts a store when it opens one.
"""
