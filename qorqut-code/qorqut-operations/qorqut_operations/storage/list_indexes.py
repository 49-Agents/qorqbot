"""Optional, explicitly migrated indexes for the lists the dashboard reads on every refresh: the newest background jobs of
a scope, the jobs running or queued in it, and the run history of a routine namespace or project.

Each index matches one statement exactly (``BackgroundService.status`` and the routine run history), so the planner has
no second index to prefer for it. The two partial indexes hold only the few jobs not yet finished; the list of newest jobs
has no ``state`` term, so they can never be chosen for it, with or without ``ANALYZE`` statistics.
"""
from .assembly import StorageContribution

OWNER = 'operations.list-indexes'
SCHEMA = (
    # Newest first with no sort, for one scope.
    "CREATE INDEX jobs_business_created ON jobs(business_id, created_at, id)",
    "CREATE INDEX jobs_business_active ON jobs(business_id, created_at) WHERE state IN ('running','applying')",
    "CREATE INDEX jobs_business_queued ON jobs(business_id, kind) WHERE state='queued'",
    "CREATE INDEX routine_runs_namespace_created ON routine_runs(namespace, created_at, id)",
    "CREATE INDEX routine_runs_business_created ON routine_runs(business_id, created_at, id)",
)
OBJECTS = frozenset({('index', 'jobs_business_created'), ('index', 'jobs_business_active'), ('index', 'jobs_business_queued'),
                     ('index', 'routine_runs_namespace_created'), ('index', 'routine_runs_business_created')})


def storage_contribution():
    return StorageContribution(OWNER, {}, schema=SCHEMA, required_tables=frozenset({'jobs', 'routine_runs'}))
