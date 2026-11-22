"""The timeline of a Task or a Project: what holds now, read off an append-only list, plus open questions.

``schema`` is the two stored tables, ``state`` calculates and renders what holds now, ``filing`` validates a set of changes and
appends them. Nothing here knows who may read or file, or that Global has no timeline; the host's
``qorqut.domains.timeline`` gates every call."""
