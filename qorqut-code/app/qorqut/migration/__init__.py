"""Retained snapshot/recovery helpers and explicit conversions for existing owner data.

``snapshots`` is the active continuity and recovery format (storage and full recovery import it). ``journal_notes``,
``project_note_tags`` and ``crm_upgrade`` are stand-alone converters that are only run on request; no service or startup path
calls them. ``crm_upgrade`` is the one schema upgrade: an offline copy of a stopped pre-CRM store into a new CRM-capable one,
recognized by the frozen ``crm_baseline_fingerprint.json``. The service never converts a store when it opens one.

``signoff_sources`` and ``queue_indexes`` are additive in-place transforms, each recognising a store that lacks only its
own change. ``upgrade`` holds them in release order and walks a store over every pending one, or back, in one transaction;
a new additive transform is appended to its ``TRANSFORMS``.
"""
