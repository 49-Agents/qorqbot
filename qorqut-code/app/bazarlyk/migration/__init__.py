"""Retained snapshot/recovery helpers and two explicit conversions for existing owner data.

``snapshots`` is the active continuity and recovery format (storage and sandbox recovery import it). ``journal_notes`` and
``project_note_tags`` are stand-alone converters that are only run on request; no service or startup path calls them.
SQL schema upgrades are not part of this package.
"""
