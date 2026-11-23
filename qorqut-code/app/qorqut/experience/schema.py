"""The stores of the Learning Applier's two workflows.

``experience_runs`` is the queue of workflow runs: one row per run of the daily experience extraction or the weekly review
and application. A run is made when its schedule comes due (``runs.py``), is started in a fresh conversation when no
other run is running, and ends when the agent finishes it or it is given up as abandoned.

``experience_observations`` holds what an extraction filed: one issue, complaint or problem each, in the agent's words,
with pointers to where it was seen. An observation states what went wrong and never a change to make. It is never
edited. Every observation is filed under one row of ``experience_clusters``, chosen when it is filed: an existing
cluster that states the same problem, or a new one. A cluster's rank is the number of observations filed under it.

``agentic_delta_proposals`` holds the solution proposals of the review: what to change in the agentic layer and why.
``agentic_delta_proposal_links`` names the observations and clusters a proposal addresses. A proposal is
``waiting_for_approval`` until the owner decides its approval request (a Signoff, ``signoff_id``), then ``approved`` or
``rejected``.
"""
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = 'experience'
RUNS = 'experience_runs'
CLUSTERS = 'experience_clusters'
OBSERVATIONS = 'experience_observations'
PROPOSALS = 'agentic_delta_proposals'
LINKS = 'agentic_delta_proposal_links'
TABLES = (RUNS, CLUSTERS, OBSERVATIONS, PROPOSALS, LINKS)
INDEXES = ('experience_runs_state', 'experience_clusters_label', 'experience_observations_cluster',
           'experience_observations_run', 'agentic_delta_proposals_state', 'agentic_delta_proposals_signoff',
           'agentic_delta_proposal_links_target')

RUN_KINDS = ('extraction', 'review')
RUN_STATES = ('queued', 'running', 'done', 'abandoned')
OBSERVATION_KINDS = ('owner_complaint', 'agent_failure', 'tool_problem', 'tool_pattern', 'other')
WAITING, APPROVED, REJECTED = 'waiting_for_approval', 'approved', 'rejected'
PROPOSAL_STATES = (WAITING, APPROVED, REJECTED)

SCHEMA = (
    '''CREATE TABLE experience_runs (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100),
 kind TEXT NOT NULL CHECK(kind IN ('extraction','review')),
 state TEXT NOT NULL CHECK(state IN ('queued','running','done','abandoned')),
 slot TEXT NOT NULL,
 window_start TEXT NOT NULL,
 window_end TEXT NOT NULL,
 agent_id TEXT REFERENCES principals(id),
 conversation_id TEXT,
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 queued_at TEXT NOT NULL,
 started_at TEXT,
 seen_at TEXT,
 finished_at TEXT,
 result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
 UNIQUE(kind, slot)
)''',
    'CREATE INDEX experience_runs_state ON experience_runs(state, slot)',
    '''CREATE TABLE experience_clusters (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100),
 label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 200),
 description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=1000),
 run_id TEXT REFERENCES experience_runs(id),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL
)''',
    'CREATE UNIQUE INDEX experience_clusters_label ON experience_clusters(lower(label))',
    '''CREATE TABLE experience_observations (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100),
 run_id TEXT REFERENCES experience_runs(id),
 cluster_id TEXT NOT NULL REFERENCES experience_clusters(id),
 kind TEXT NOT NULL CHECK(kind IN ('owner_complaint','agent_failure','tool_problem','tool_pattern','other')),
 subject TEXT NOT NULL CHECK(length(subject) BETWEEN 1 AND 200),
 text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 2000),
 evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_json)),
 occurred_at TEXT,
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL
)''',
    'CREATE INDEX experience_observations_cluster ON experience_observations(cluster_id, created_at)',
    'CREATE INDEX experience_observations_run ON experience_observations(run_id)',
    '''CREATE TABLE agentic_delta_proposals (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100),
 number INTEGER NOT NULL UNIQUE CHECK(number>=1),
 run_id TEXT REFERENCES experience_runs(id),
 replaces_id TEXT REFERENCES agentic_delta_proposals(id),
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 problem TEXT NOT NULL CHECK(length(problem) BETWEEN 1 AND 2000),
 change TEXT NOT NULL CHECK(length(change) BETWEEN 1 AND 4000),
 state TEXT NOT NULL DEFAULT 'waiting_for_approval' CHECK(state IN ('waiting_for_approval','approved','rejected')),
 signoff_id TEXT,
 requests INTEGER NOT NULL DEFAULT 0 CHECK(requests>=0),
 decided_at TEXT,
 decided_by TEXT REFERENCES principals(id),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK((state='waiting_for_approval')=(decided_at IS NULL))
)''',
    'CREATE INDEX agentic_delta_proposals_state ON agentic_delta_proposals(state, created_at)',
    'CREATE INDEX agentic_delta_proposals_signoff ON agentic_delta_proposals(signoff_id)',
    '''CREATE TABLE agentic_delta_proposal_links (
 proposal_id TEXT NOT NULL REFERENCES agentic_delta_proposals(id),
 target_type TEXT NOT NULL CHECK(target_type IN ('observation','cluster')),
 target_id TEXT NOT NULL CHECK(length(target_id) BETWEEN 1 AND 100),
 PRIMARY KEY(proposal_id, target_type, target_id)
)''',
    'CREATE INDEX agentic_delta_proposal_links_target ON agentic_delta_proposal_links(target_type, target_id)',
)


def storage_contribution() -> StorageContribution:
    plain = TableSpec(('id',), mutable=True, revision_column=None)
    return StorageContribution(OWNER, {
        RUNS: plain, CLUSTERS: plain, OBSERVATIONS: TableSpec(('id',), mutable=False, revision_column=None), PROPOSALS: plain,
        LINKS: TableSpec(('proposal_id', 'target_type', 'target_id'), mutable=True, revision_column=None)}, schema=SCHEMA)
