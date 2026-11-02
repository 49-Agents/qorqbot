"""Business operations implemented against the shared application contracts."""

from dataclasses import dataclass
from .dev_tickets import DevTickets
from .viewbox import AgentViewbox
from .document_history import DocumentHistory
from .file_links import FileLinks
from .documents import DocumentOperations
from .whatsapp_send import WhatsAppSendOperations
from .tasks import task_ports
from qorqut_context.integration.filing.source_operations import ContextSourceOperations
from qorqut_context.integration.queue import ContextQueueOperations
from qorqut_context.integration.questions import ContextQuestionOperations, QuestionStore
from qorqut_context.integration.reading import SharedContextReader, shared_context_contribution
from qorqut_context.integration.usage import ContextUsageOperations
from qorqut_operations.actions import ActionOperations
from qorqut_operations.actions.writeback import ActionWriteback
from qorqut_operations.assets import AssetOperations
from qorqut_operations.items import ItemResolver
from qorqut_operations.projects.archive import BusinessArchive
from qorqut_operations.projects.creation import BusinessCreationOperations
from qorqut_operations.tags import TagOperations
from qorqut_operations.tables import DataUploadOperations, TableOperations
from qorqut_operations.tasks import TaskOperations
from qorqut_operations.relationships import ContextReviews, RelationshipOperations
from bazarlyk.composition.locations import business_hq, business_root, relative_to_operations
from bazarlyk.instance_paths import PROJECTS_DIRECTORY
from bazarlyk.extensions import NO_EXTENSIONS
from .items.compose import build_neutral_items
from .items.resources import RelatedResources
from qorqut_operations.application.plans import utc_clock
from bazarlyk.composition.catalog import install_contribution
from .business_creation.access import creator_actor
from .costs import CostOperations
from .working_memory import WorkingMemory
from .owner_actions import OwnerActions
from .business_progress import BusinessProgress
from .business_briefing import BusinessBriefing
from bazarlyk.agents.roles import LEARNING_APPLIER_ROLE
from bazarlyk.incidents import IncidentOperations
from bazarlyk.shared_context.service import INSTRUCT_ROLES


@dataclass(frozen=True)
class BusinessDomains:
    tasks: TaskOperations
    activity: ActionWriteback
    documents: DocumentOperations
    assets: AssetOperations
    tables: TableOperations
    data_uploads: DataUploadOperations
    relationships: RelationshipOperations
    items: ItemResolver
    related: RelatedResources
    reviews: ContextReviews
    business_creation: BusinessCreationOperations
    working_memory: WorkingMemory
    archive: BusinessArchive
    context_sources: ContextSourceOperations = None
    context_questions: ContextQuestionOperations = None
    shared_context: SharedContextReader = None
    viewbox: AgentViewbox = None
    actions: ActionOperations = None
    tags: TagOperations = None
    queue: ContextQueueOperations = None

    def resolve_file_target(self, resource):
        if resource.resource_type == "business_creation_directories":
            return self.business_creation.creation.directory_target(resource)
        return self.documents.store.resolve_file_target(resource)


def no_additional_asset_references(row, ctx, uow):
    """The host has no source of ``used_by`` entries for an asset beyond the Operations documents that name it."""
    return []


def register_business_domains(registry, application, paths, *, agent_resolver, queue, on_revision, catalog=None, clock=utc_clock,
                              job_programs=None, filing=None, graph_edits=None, extensions=NO_EXTENSIONS):
    """Register every business domain. ``queue`` is the composed Context queue and ``on_revision`` the one Action revision
    admission (``ContextComposition.on_revision``) that ``ActionOperations``, the surviving Action writer, settles through;
    both are required."""
    if queue is None:
        raise TypeError("The business domains need the composed Context queue (ContextComposition.queue)")
    tables = TableOperations(application.database, paths.operations, lambda business: business_hq(paths, business), clock=clock, job_programs=job_programs)
    activity = ActionWriteback(clock=clock)
    documents = DocumentOperations(application.database, paths, clock=clock, job_programs=job_programs, filing=filing)
    assets = AssetOperations(documents.store, additional_references=no_additional_asset_references, clock=clock)
    data_uploads = DataUploadOperations(documents.store, clock=clock)
    DevTickets().register(registry)
    viewbox = AgentViewbox(application, clock=clock).register(registry)
    IncidentOperations(application).register(registry)
    BusinessProgress(clock=clock).register(registry)
    BusinessBriefing(application, clock=clock).register(registry)
    tables.register(registry)
    context_sources = ContextSourceOperations(filing=documents.filing, clock=documents.clock, graph_edits=graph_edits,
                                              instruct_roles=INSTRUCT_ROLES).register(registry)
    context_questions = ContextQuestionOperations(application, lambda: QuestionStore(paths.context_questions),
                                                  learning_applier_role=LEARNING_APPLIER_ROLE).register(registry)
    ContextUsageOperations().register(registry)
    # The one published Context read engine: registered directly over the instance's graph locations, with no
    # per-business reads beside it. ``documents.shared_context`` is the reader the other host read consumers use.
    shared_context = SharedContextReader(paths.context, paths.context_runtime)
    install_contribution(registry, shared_context_contribution(shared_context))
    DocumentHistory(documents).register(registry)
    FileLinks(documents, paths).register(registry)
    assets.register(registry)
    data_uploads.register(registry)
    WhatsAppSendOperations(application, paths, job_programs).register(registry)
    neutral = build_neutral_items(application, documents=documents, clock=clock, extensions=extensions.item_kinds())
    # The engine's operational records: the shared tag catalog, Tasks (over the neutral items and the host's ports),
    # Actions and the Context input queue. Their handlers install here.
    tags = TagOperations(clock=clock)
    tasks = TaskOperations(application, neutral.items, task_ports(application, extensions.note_derivations()), clock=clock)
    actions = ActionOperations(application, tasks.note_references(), on_revision=on_revision, clock=clock)
    for contribution in (tags.contribution(), tasks.contribution(), actions.contribution(), queue.contribution()):
        install_contribution(registry, contribution)
    OwnerActions(paths, clock=clock).register(registry)
    install_contribution(registry, neutral.relationships.contribution())
    archive = BusinessArchive(application)
    archive.register(registry)
    business_creation = BusinessCreationOperations(
        application,
        operations_root=paths.operations,
        staging_root=paths.runtime / "staging" / PROJECTS_DIRECTORY,
        project_root=lambda row: business_root(paths, row),
        project_hq=lambda row: business_hq(paths, row),
        relative_path=lambda path: relative_to_operations(paths, path),
        projects_directory=PROJECTS_DIRECTORY,
        creator_actor=creator_actor,
    )
    business_creation.register(registry)
    CostOperations(clock=clock).register(registry)
    memory = WorkingMemory(application, documents, agent_resolver, clock=clock)
    memory.register(registry)
    if agent_resolver is not None:
        agent_resolver.working_memory = memory
    documents.register(registry, application, working_memory=memory, shared_context=shared_context)
    return BusinessDomains(tasks, activity, documents, assets, tables, data_uploads, neutral.relationships, neutral.items, neutral.related, neutral.reviews, business_creation, memory, archive, context_sources=context_sources, context_questions=context_questions, shared_context=shared_context, viewbox=viewbox, actions=actions, tags=tags, queue=queue)
