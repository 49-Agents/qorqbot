"""Project records: one existing Project's inventory, neutral references and explicit cross-scope links.

``ProjectRecords`` registers the six ``project_*`` operations and owns the one Operations link store. The
``project`` neutral item kind is contributed by ``qorqut.domains.items`` (``ProjectLookup``) into the single
frozen item resolver.
"""

from .compose import ProjectRecords

__all__ = ["ProjectRecords"]
