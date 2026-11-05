"""Project creation: the configured Ordubashy creates one empty local project.

The one Global operation ``business_create`` (the internal operation, route and table names keep ``business``) takes a name, an
optional description (``summary``) and an optional folder slug. The only caller is the installation's configured, active
Ordubashy holding ``coordinate_businesses`` (plus the ordinary operation grant and policy). The installation owner, every other
human or agent (including a worker with an old setup capability or an explicit allow) and every system program are refused at
preparation, validation and application, and again on recovery: the owner asks Ordubashy. The owner is still the new project's
owner member. Nothing here starts a session, a provider action, a resident agent or any provisioning."""
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, TEXT, schema
from qorqut_operations.contracts import DomainError
from .creation import Creation

# The Global capability the operation descriptor requires. Holding it is necessary and never sufficient: the creator is
# decided by the host-supplied ``creator_actor(uow, identity)`` on every phase.
COORDINATION_CAPABILITY = "coordinate_businesses"

# The name is trimmed and bounded to 200 characters when the plan is prepared (surrounding whitespace is not counted).
NAME = {"type": "string", "minLength": 1, "maxLength": 1000}
SLUG = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"}
# The project's Description: short, optional, and empty is allowed.
SUMMARY = {"type": "string", "maxLength": 2000}
PROJECT = schema({"id": {"type": "string"}, "slug": {"type": "string"}, "name": {"type": "string"}, "summary": {"type": "string"},
                  "workspace_path": {"type": "string"}, "hq_path": {"type": "string"}},
                 ["id", "slug", "name", "summary", "workspace_path", "hq_path"])
OUTPUT = schema({"business": PROJECT, "business_id": {"type": "string"}, "slug": {"type": "string"}}, ["business", "business_id", "slug"])
VERSION = 2
DESCRIPTION = (
    "Create a new Project: an empty container for Tasks, documents, tables, assets and code. Supply name (required, up to 200 "
    "characters), optionally summary (the project's Description, up to 2000 characters, may be empty) and optionally slug (the "
    "folder name under projects/: lowercase letters, digits and hyphens, starting with a letter, up to 64 characters; when omitted "
    "it is derived from the name). A later name change never renames the folder. Creation makes only the project record, the "
    "owner's membership and an empty projects/<slug> folder with hq/ and code/: no agents, documents, Tasks, routines, Context "
    "inputs, external accounts or domains, and no session is started. If the folder name is taken the call fails with "
    "revision_conflict and the available suggested_slug; nothing existing is ever adopted. Only the configured Ordubashy can "
    "create projects; the owner and other agents ask Ordubashy. The response returns business (id, slug, name, summary, "
    "workspace_path, hq_path), business_id and slug. Retry the same idempotency_key after interruption.")


class BusinessCreationOperations:
    """The ``business_create`` operation over explicit host facts; it reads no installation paths of its own.

    ``operations_root`` is the installation's operations directory and ``staging_root`` its private project-folder staging
    area; ``project_root(row)`` and ``project_hq(row)`` are the host's validating location functions for a registered
    project row (``{"slug": ...}``), ``relative_path(path)`` the operations-relative form of an absolute location, and
    ``projects_directory`` the projects directory's name. The functions are called at each validation point, never cached.
    ``creator_actor(uow, identity)`` returns the creator's role when ``identity`` is the one identity allowed to create
    projects, else ``None``; it is required and is consulted afresh on every phase."""

    def __init__(self, application, *, operations_root, staging_root, project_root, project_hq, relative_path,
                 projects_directory, creator_actor):
        self.application = application
        self.operations_root, self.staging_root = operations_root, staging_root
        self.project_root, self.project_hq, self.relative_path = project_root, project_hq, relative_path
        self.projects_directory, self.creator_actor = projects_directory, creator_actor
        # The bounded system programs are registered with the background service, which is attached after the domains.
        self.worker_authority = None
        self.creation = Creation(self)

    def register(self, registry):
        properties = {"name": NAME, "slug": SLUG, "summary": SUMMARY, "rationale": TEXT}
        registry.register(OperationDescriptor("business_create", schema(properties, ["name"]), OUTPUT, version=VERSION,
            scope_kinds=frozenset({"global"}), global_capability=COORDINATION_CAPABILITY, effect_kind="compound",
            description=DESCRIPTION, prepare=self.prepare, apply=self.apply, validate_targets=self.validate))
        self.creation.register_targets()

    def access(self, ctx, uow):
        """Refuse everyone but the configured, active Ordubashy; re-checked on every phase, including recovery.

        There is no owner path, no forwarding and no direct-apply bypass: the identity is resolved from the installation's
        configured Ordubashy and its stored active profile each time, never from the transport, a name or a role label."""
        if ctx.business_id is not None or ctx.home_business_id is not None:
            raise DomainError("forbidden", "Project creation is a Global operation; do not select a project")
        if ctx.principal_kind == "agent" and self.creator_actor(uow, ctx.principal_id) is not None:
            return
        raise DomainError("forbidden", "Only the configured Ordubashy can create projects; ask Ordubashy to create it")

    def prepare(self, ctx, args, opts, uow):
        self.access(ctx, uow)
        builder = PlanBuilder(ctx, "business_create", args, "Project Create", namespace="global", affected_scope={"kind": "global"},
                              version=VERSION)
        return self.creation.prepare(builder, args, uow)

    def validate(self, ctx, plan, uow):
        self.access(ctx, uow)
        self.creation.validate(ctx, plan, uow)

    def apply(self, ctx, plan, uow):
        self.access(ctx, uow)
        return self.creation.apply(ctx, plan, uow)

