"""Same-transaction finalization and post-commit change publication.

Order inside a writing unit (the engine's): the body, ``ChangeFeed.capture`` (reads the pending ``inbox_changes``
rows), then ``finalize_events`` (turns them into inbox events and deletes them), all before ``COMMIT``; a failure in
either rolls the unit back. ``ChangeFeed.publish_commit`` runs once after the commit and never undoes or fails it."""

from __future__ import annotations

from functools import partial

from qorqut.inbox.events import finalize_events
from qorqut.service.changes import ChangeFeed


def inbox_finalizers(sources=None):
    """The one inbox finalizer, over the frozen source handlers of the selected extensions (none for a core-only host)."""
    return (partial(finalize_events, sources=sources) if sources else finalize_events,)


def require_change_feed(publisher) -> ChangeFeed:
    if not isinstance(publisher, ChangeFeed):
        raise TypeError("The host change publisher is its ChangeFeed")
    return publisher
