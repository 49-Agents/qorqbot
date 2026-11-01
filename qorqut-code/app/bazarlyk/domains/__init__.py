"""Business operations implemented against the shared application contracts."""

from dataclasses import dataclass
from .activity import ActivityOperations
from .dev_tickets import DevTickets
from .viewbox import AgentViewbox
from .document_history import DocumentHistory
from .file_links import FileLinks
from .assets import AssetOperations
from .documents import DocumentOperations
from .contextbase import ContextBaseOperations
from .email import EmailOperations
from .account_email import AccountEmailOperations
from .ledger import LedgerOperations
from .metrics import MetricOperations
from .platform_actions.operations import PlatformActions
from .tasks import TaskOperations
from .tables import TableOperations
from .tables.uploads import DataUploadOperations
from .stories import StoryOperations
from .products import ProductOperations
from .relationships import RelationshipOperations
from .common import utc_clock
from .ideas import IdeaOperations
from .personal_context import PersonalContextOperations
from .subscriptions import SubscriptionOperations
from .costs import CostOperations
from .working_memory import WorkingMemory
from .owner_actions import OwnerActions
from .milestones import MilestoneOperations
from .business_progress import BusinessProgress
from .business_briefing import BusinessBriefing
from .business_archive import BusinessArchive
from .business_setup import BusinessSetupOperations
from bazarlyk.incidents import IncidentOperations


@dataclass(frozen=True)
class BusinessDomains:
    tasks: TaskOperations
    ledger: LedgerOperations
    metrics: MetricOperations
    activity: ActivityOperations
    documents: DocumentOperations
    assets: AssetOperations
    platform_actions: PlatformActions
    email: EmailOperations
    stories: StoryOperations
    tables: TableOperations
    data_uploads: DataUploadOperations
    relationships: RelationshipOperations
    ideas: IdeaOperations
    personal_context: PersonalContextOperations
    subscriptions: SubscriptionOperations
    working_memory: WorkingMemory
    archive: BusinessArchive
    context_base: ContextBaseOperations = None
    business_setup: BusinessSetupOperations = None
    viewbox: AgentViewbox = None

    def resolve_file_target(self, resource):
        if resource.resource_type == "idea_creation_files":
            return self.ideas.conversion.file_target(resource)
        return self.documents.store.resolve_file_target(resource)


def register_business_domains(registry, application, businesses_root, *, catalog, agent_resolver, clock=utc_clock, job_programs=None):
    tasks = TaskOperations(clock=clock)
    tables = TableOperations(application.database, businesses_root, clock=clock, job_programs=job_programs)
    ledger = LedgerOperations(tables=tables, clock=clock, job_programs=job_programs)
    metrics = MetricOperations(tables=tables, clock=clock, job_programs=job_programs)
    activity = ActivityOperations(ledger=ledger, clock=clock)
    documents = DocumentOperations(application.database, businesses_root, tasks=tasks, activity=activity, clock=clock, job_programs=job_programs)
    assets = AssetOperations(documents.store, clock=clock)
    data_uploads = DataUploadOperations(documents.store, clock=clock)
    email = EmailOperations(application=application, resolver=agent_resolver, documents=documents, clock=clock, programs=job_programs)
    account_email = AccountEmailOperations(email, job_programs)
    platform_actions = PlatformActions(application=application, catalog=catalog, agent_resolver=agent_resolver, documents=documents, tasks=tasks, activity=activity, clock=clock, job_programs=job_programs, email=email)
    DevTickets().register(registry)
    viewbox = AgentViewbox(application, clock=clock).register(registry)
    IncidentOperations(application).register(registry)
    tasks.register(registry, application)
    MilestoneOperations(clock=clock).register(registry)
    BusinessProgress(clock=clock).register(registry)
    BusinessBriefing(application, clock=clock).register(registry)
    tables.register(registry, application)
    ledger.register(registry)
    metrics.register(registry)
    activity.register(registry)
    documents.register(registry, application)
    context_base = ContextBaseOperations(documents, job_programs=job_programs).register(registry)
    documents.context_base = context_base
    DocumentHistory(documents).register(registry)
    FileLinks(documents, businesses_root).register(registry)
    from .documents.journal import JournalOperations
    documents.journal = JournalOperations(documents).register(registry)
    assets.register(registry)
    data_uploads.register(registry)
    platform_actions.register(registry)
    email.register(registry)
    account_email.register(registry)
    from .email.unified import UnifiedEmailOperations
    UnifiedEmailOperations(email=email, account=account_email, platform=platform_actions,
                           resolver=agent_resolver).register(registry)
    stories = StoryOperations(documents=documents, tasks=tasks, clock=clock)
    stories.register(registry, application)
    OwnerActions(businesses_root, stories, clock=clock).register(registry)
    tasks.stories = stories
    documents.stories = stories
    assets.stories = stories
    tables.stories = stories
    data_uploads.stories = stories
    ProductOperations(application, clock=clock).register(registry)
    relationships = RelationshipOperations(stories, application, clock=clock)
    stories.relationships = relationships
    relationships.register(registry)
    business_setup = BusinessSetupOperations(application, tasks, clock=clock)
    business_setup.register(registry, relationships)
    email.setup_reconcile = business_setup.reconcile
    archive = BusinessArchive(application)
    archive.resolver = agent_resolver
    archive.register(registry)
    ideas = IdeaOperations(application, agent_resolver, documents)
    ideas.register(registry)
    personal_context = PersonalContextOperations(documents)
    personal_context.register(registry)
    subscriptions = SubscriptionOperations()
    subscriptions.register(registry)
    CostOperations(context_base, clock=clock).register(registry)
    memory = WorkingMemory(application, documents, email, agent_resolver, clock=clock)
    memory.register(registry)
    if agent_resolver is not None:
        agent_resolver.working_memory = memory
    documents.working_memory = memory
    return BusinessDomains(tasks, ledger, metrics, activity, documents, assets, platform_actions, email, stories, tables, data_uploads, relationships, ideas, personal_context, subscriptions, memory, archive, context_base=context_base, business_setup=business_setup, viewbox=viewbox)
