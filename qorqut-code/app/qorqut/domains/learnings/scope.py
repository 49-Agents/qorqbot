"""Explicit workspace ownership for learning operations."""
from qorqut_operations.application.plans import PlanBuilder as BaseBuilder
from qorqut_operations.contracts import DomainError
from qorqut_operations.records.locate import LOCATED, Family, RecordLookup

# One Global learning stream (v1.2 G022). Project learning records stay readable and reviewable as history; nothing new
# is extracted, enrolled or recorded into a project.
GLOBAL_ONLY = "Learning extraction runs only in Global; project learning records are kept unchanged as history"


def require_global(project_id, message=GLOBAL_ONLY):
    """Refuse a project scope before any job, transcript read, model call, engine work or learning record change."""
    if project_id is not None:
        raise DomainError("forbidden", message)


def namespace(ctx, args=None):
    return 'project:' + ctx.project_id if ctx.project_id else 'global'


SCOPE = dict(scope_kinds={'project', 'global'}, global_capability='read_global_context', resolve_namespace=namespace)


class PlanBuilder(BaseBuilder):
    def __init__(self, ctx, *args, **kwargs):
        super().__init__(ctx, *args, namespace=namespace(ctx),
                         affected_scope={'kind': 'project', 'project_id': ctx.project_id} if ctx.project_id else {'kind': 'global'}, **kwargs)


def learning_lookup():
    """The ``locate_scope`` hook of every operation that names one learning by ``learning_id``. These operations take no scope
    argument: selecting no project is Global, so a learning found in Global needs nothing stated."""
    return RecordLookup(Family("learning_candidates", "learning_id", "learnings_get", "Learning", scope_argument=None))


def located(lookup, description=""):
    """The descriptor fields of an operation that names one learning by its id."""
    return {"locate_scope": lookup, "description": description + LOCATED if description else LOCATED.lstrip()}
