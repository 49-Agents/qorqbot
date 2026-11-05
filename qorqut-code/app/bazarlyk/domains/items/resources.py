"""The host's resource tables beside the Operations related-resource resolver: the authorized read of the configured-global
agent profile.

``HostResources`` is the ``ResourceHost`` of ``qorqut_operations.records.resources``. The active supported global's reach
(never an agent membership) is the host's; resolving a resource grants nothing the caller's policy would not."""

from qorqut_operations.application.plans import ref
from qorqut_operations.contracts import DomainError
from qorqut_operations.records.resources import READ_OPERATIONS as RECORD_READ_OPERATIONS, RelatedResources

from bazarlyk.agents.global_scope import active_global, reaches_business

# The read operation that authorizes each resource table: the neutral ones.
READ_OPERATIONS = dict(RECORD_READ_OPERATIONS)


class HostResources:
    def __init__(self, application):
        self.application = application

    def read_special(self, ctx, uow, kind, identity, builder):
        if kind == "agent_profiles":
            # An agent is an active supported global, read without a home: in a selected business it needs only its
            # derived reach to that active business. Its principal and profile are pinned, never a membership.
            row = uow.repository(kind, business_id=None).get(identity, required=True)
            principal = uow.repository("principals").get(identity)
            if active_global(uow.connection, identity) is None or (
                    ctx.business_id is not None and not reaches_business(uow.connection, identity, ctx.business_id)):
                raise DomainError("invalid_assignee", "Assign an active agent in this project")
            if builder:
                builder.expect(ref("principals", identity, None), principal)
                builder.expect(ref(kind, identity, None, "ecosystem" if ctx.business_id else None), row)
            return row
        return None

    def project_special(self, kind, row):
        return None


def build_related(application, documents) -> RelatedResources:
    """The host's one related-resource resolver, over the actual Application and document service."""
    return RelatedResources(application, documents, host=HostResources(application), read_operations=READ_OPERATIONS)
