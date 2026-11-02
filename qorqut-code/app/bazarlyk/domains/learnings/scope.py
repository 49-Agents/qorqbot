"""Explicit workspace ownership for learning operations."""
from qorqut_operations.application.plans import PlanBuilder as BaseBuilder
from qorqut_operations.contracts import DomainError

# One Global learning stream (v1.2 G022). Business learning records stay readable and reviewable as history; nothing new
# is extracted, enrolled or recorded into a business.
GLOBAL_ONLY = "Learning extraction runs only in Global; business learning records are kept unchanged as history"


def require_global(business_id, message=GLOBAL_ONLY):
    """Refuse a business scope before any job, transcript read, model call, engine work or learning record change."""
    if business_id is not None:
        raise DomainError("forbidden", message)


def namespace(ctx, args=None):
    return 'business:' + ctx.business_id if ctx.business_id else 'global'


SCOPE = dict(scope_kinds={'business', 'global'}, global_capability='read_global_context', resolve_namespace=namespace)


class PlanBuilder(BaseBuilder):
    def __init__(self, ctx, *args, **kwargs):
        super().__init__(ctx, *args, namespace=namespace(ctx),
                         affected_scope={'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'}, **kwargs)
