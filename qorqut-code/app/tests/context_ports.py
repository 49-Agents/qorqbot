"""The Context ports the stock host composes once and hands to ``register_business_domains``: one queue and the Action revision
admission. Focused fixtures that register the business domains without the full runtime use this instead of ``compose_context``."""

from qorqut_context.integration.filing import FilingOutcomes
from qorqut_context.integration.graph_edits import ExecutionFacts
from qorqut_context.integration.queue import ContextQueueOperations, admit_action_revision
from qorqut_operations.application.plans import utc_clock


def context_ports(clock=utc_clock):
    """``queue=`` and ``on_revision=`` for ``register_business_domains``."""
    queue = ContextQueueOperations(outcomes=FilingOutcomes(), clock=clock, executions=ExecutionFacts())
    return {"queue": queue, "on_revision": admit_action_revision}
