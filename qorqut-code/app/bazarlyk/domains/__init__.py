"""Business operations implemented against the shared application contracts."""

from dataclasses import dataclass
from .activity import ActionWriteback
from .dev_tickets import DevTickets
from .viewbox import AgentViewbox
from .document_history import DocumentHistory
from .file_links import FileLinks
from .assets import AssetOperations
from .documents import DocumentOperations
from .context_sources import ContextSourceOperations
from bazarlyk.context_questions import ContextQuestionOperations, QuestionStore
from .email import EmailOperations
from .account_email import AccountEmailOperations
from .whatsapp_send import WhatsAppSendOperations
from .platform_actions.operations import PlatformActions
from .tasks import task_ports
from .tables import TableOperations
from .tables.uploads import DataUploadOperations
from qorqut_operations.actions import ActionOperations
from qorqut_operations.context_filing import FilingOutcomes
from qorqut_operations.context_graph_edits import ExecutionFacts
from qorqut_operations.context_queue import ContextQueueOperations
from qorqut_operations.items import ItemResolver
from qorqut_operations.tags import TagOperations
from qorqut_operations.tasks import TaskOperations
from qorqut_operations.relationships import ContextReviews, RelationshipOperations
from .items.compose import build_neutral_items
from .items.resources import RelatedResources
from qorqut_operations.application.plans import utc_clock
from bazarlyk.composition.catalog import install_contribution
from bazarlyk.shared_context import SharedContextReader, shared_context_contribution
from .business_creation import BusinessCreationOperations
from .personal_context import PersonalContextOperations
from .costs import CostOperations
from bazarlyk.context_usage import ContextUsageOperations
from .working_memory import WorkingMemory
from .owner_actions import OwnerActions
from .milestones import MilestoneOperations
from .business_progress import BusinessProgress
from .business_briefing import BusinessBriefing
from .business_archive import BusinessArchive
from bazarlyk.incidents import IncidentOperations


@dataclass(frozen=True)
class BusinessDomains:
    tasks: TaskOperations
    activity: ActionWriteback
    documents: DocumentOperations
    assets: AssetOperations
    platform_actions: PlatformActions
    email: EmailOperations
    tables: TableOperations
    data_uploads: DataUploadOperations
    relationships: RelationshipOperations
    items: ItemResolver
    related: RelatedResources
    reviews: ContextReviews
    business_creation: BusinessCreationOperations
    personal_context: PersonalContextOperations
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


def register_business_domains(registry, application, paths, *, catalog, agent_resolver, clock=utc_clock, job_programs=None, filing=None,
                              graph_edits=None, queue=None):
    tables = TableOperations(application.database, paths, clock=clock, job_programs=job_programs)
    activity = ActionWriteback(clock=clock)
    documents = DocumentOperations(application.database, paths, clock=clock, job_programs=job_programs, filing=filing)
    assets = AssetOperations(documents.store, clock=clock)
    data_uploads = DataUploadOperations(documents.store, clock=clock)
    email = EmailOperations(application=application, resolver=agent_resolver, documents=documents, clock=clock, programs=job_programs)
    account_email = AccountEmailOperations(email, job_programs)
    # The Task service is built once the neutral items exist.
    platform_actions = PlatformActions(application=application, catalog=catalog, agent_resolver=agent_resolver, documents=documents, tasks=None, activity=activity, clock=clock, job_programs=job_programs, email=email)
    DevTickets().register(registry)
    viewbox = AgentViewbox(application, clock=clock).register(registry)
    IncidentOperations(application).register(registry)
    MilestoneOperations(clock=clock).register(registry)
    BusinessProgress(clock=clock).register(registry)
    BusinessBriefing(application, clock=clock).register(registry)
    tables.register(registry)
    documents.register(registry, application)
    context_sources = ContextSourceOperations(documents, job_programs=job_programs, graph_edits=graph_edits).register(registry)
    context_questions = ContextQuestionOperations(application, lambda: QuestionStore(paths.context_questions)).register(registry)
    ContextUsageOperations().register(registry)
    # The one published Context read engine: registered directly over the instance's graph locations, with no
    # per-business reads beside it. ``documents.shared_context`` is the reader the other host read consumers use.
    shared_context = SharedContextReader.for_instance(paths)
    install_contribution(registry, shared_context_contribution(shared_context))
    documents.shared_context = shared_context
    DocumentHistory(documents).register(registry)
    FileLinks(documents, paths).register(registry)
    from .documents.journal import JournalOperations
    documents.journal = JournalOperations(documents).register(registry)
    assets.register(registry)
    data_uploads.register(registry)
    platform_actions.register(registry)
    email.register(registry)
    account_email.register(registry)
    WhatsAppSendOperations(application, paths, job_programs).register(registry)
    from .email.unified import UnifiedEmailOperations
    UnifiedEmailOperations(email=email, account=account_email, platform=platform_actions,
                           resolver=agent_resolver).register(registry)
    neutral = build_neutral_items(application, documents=documents, email=email, clock=clock)
    # The engine's operational records: the shared tag catalog, Tasks (over the neutral items and the host's ports),
    # Actions and the Context input queue. Their handlers install here.
    tags = TagOperations(clock=clock)
    tasks = TaskOperations(application, neutral.items, task_ports(application), clock=clock)
    actions = ActionOperations(application, tasks.note_references(), clock=clock)
    # The caller that wires ``filing`` or ``graph_edits`` hands in the one queue those ports and the driver share; otherwise a
    # queue with its own outcome and execution facts is built here.
    if queue is None:
        queue = ContextQueueOperations(outcomes=FilingOutcomes(), clock=clock, executions=ExecutionFacts())
    for contribution in (tags.contribution(), tasks.contribution(), actions.contribution(), queue.contribution()):
        install_contribution(registry, contribution)
    platform_actions.tasks = tasks
    OwnerActions(paths, clock=clock).register(registry)
    install_contribution(registry, neutral.relationships.contribution())
    archive = BusinessArchive(application)
    archive.resolver = agent_resolver
    archive.register(registry)
    business_creation = BusinessCreationOperations(application, paths)
    business_creation.register(registry)
    personal_context = PersonalContextOperations(documents)
    personal_context.register(registry)
    CostOperations(clock=clock).register(registry)
    memory = WorkingMemory(application, documents, email, agent_resolver, clock=clock)
    memory.register(registry)
    if agent_resolver is not None:
        agent_resolver.working_memory = memory
    documents.working_memory = memory
    return BusinessDomains(tasks, activity, documents, assets, platform_actions, email, tables, data_uploads, neutral.relationships, neutral.items, neutral.related, neutral.reviews, business_creation, personal_context, memory, archive, context_sources=context_sources, context_questions=context_questions, shared_context=shared_context, viewbox=viewbox, actions=actions, tags=tags, queue=queue)
