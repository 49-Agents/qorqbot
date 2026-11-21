"""The thin host aggregate of the document domain: construction and registration of its owners, nothing else.

Every document operation has exactly one owner and is registered by it:

* ``qorqut_operations.documents.operations.DocumentSources`` (Operations): the source reads and the source authoring
  operations (``notes_*``, ``content_*``, ``context_people_*``, ``context_topics_*``, ``context_notes_*``,
  ``context_document_update``, ``context_maps_get`` and the scoped ``context_list``/``context_get``/``context_search``);
* ``qorqut_operations.documents.journal.JournalOperations`` (Operations): ``journal_entries_list``, ``journal_entry_get``;
* ``qorqut_context.integration.filing.operations.FilingOperations`` (Context): ``context_upload``, ``notes_upload``,
  ``journal_upload_to_filer``, ``context_retry``, ``context_retract``, ``context_filing_receipt``;
* ``shared_instructions.SharedInstructions`` (host, permanent): ``shared_instructions_update``;
* ``project_config.ProjectConfigOperations`` (the host's binding of ``qorqut_operations.projects.settings_operations``, which owns the
  generic Project settings operations; the host supplies only its guards through ``HostSettings``): ``project_config_*``.

The aggregate holds no behavior of its own. The attributes other host code reads (``store``, ``library``, ``filing``, ``clock``)
and the few filing projections they share are plain, explicit delegations; there is no attribute forwarding. The published
Context reader is a host service built after the documents; ``register`` takes it explicitly, so a composition that forgot it
fails here instead of silently skipping a check."""

from __future__ import annotations

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.documents.journal import JournalOperations
from qorqut_operations.documents.library import SourceLibrary
from qorqut_operations.documents.operations import DocumentSources
from qorqut_operations.documents.store import DocumentStore

from qorqut_context.integration.filing.filing import ContextFiling
from qorqut_context.integration.filing.operations import FilingOperations
from qorqut_context.integration.filing.publication import ContextPublication

from qorqut.composition.locations import document_root
from qorqut.management.identity import configured_ordubashy
from .project_config import ProjectConfigOperations
from .shared_instructions import SharedInstructions


class DocumentOperations:
    def __init__(self, database, paths, *, clock=utc_clock, job_programs=None, filing=None):
        self.clock, self.programs = clock, job_programs
        self.store = DocumentStore(database, operations_root=paths.operations,
                                   document_root=lambda project, path: document_root(paths, project, path))
        # Context filing (uploads, notes, journals, accepted learnings and instructions) is an explicit typed dependency:
        # ``filing`` is the ``FilingPorts`` of the composed Context queue. Without it filing operations are unavailable.
        self.filing = None if filing is None else ContextFiling(self.store, job_programs, filing)
        self.publication = ContextPublication(self.filing)
        # The scoped library presents a raw upload with the same current-source filing the unscoped reads attach.
        self.library = SourceLibrary(self.store, present=self.publication.present_item)
        self.sources = DocumentSources(self.store, self.library, clock=clock, present=self.publication.present_view)
        self.filing_operations = FilingOperations(self.sources, self.filing,
            global_upload_agent=lambda uow: configured_ordubashy(uow))
        self.project_config = ProjectConfigOperations(paths, clock=clock)
        self.journal = JournalOperations(self.store, lambda uow: configured_ordubashy(uow), sources=self.sources)
        self.shared_instructions = self.shared_context = None

    def register(self, registry, application, *, shared_context):
        if shared_context is None:
            raise TypeError("The document domain needs the host's published Context reader to register")
        self.shared_context = shared_context
        self.shared_instructions = SharedInstructions(self.sources)
        self.sources.register(registry, application)
        self.filing_operations.register(registry)
        self.shared_instructions.register(registry)
        self.project_config.register(registry)
        self.journal.register(registry)
        return self

    # Explicit delegations other host code reads.

    def validate_targets(self, ctx, plan, uow):
        """The registered-document checks a host workflow that changes documents (accepted learnings) runs on its own plan."""
        return self.sources.validate_targets(ctx, plan, uow)

    def require_filing(self):
        return self.filing_operations.require_filing()

    def job_filing(self, uow, ctx, job):
        return self.publication.job_filing(uow, ctx, job)

    def source_filing(self, uow, ctx, row):
        return self.publication.source_filing(uow, ctx, row)

    def present(self, view, row, ctx, uow):
        return self.publication.present_view(view, row, ctx, uow)

    def prepare_learning_intake(self, ctx, args, uow, *, metadata, source_refs):
        return self.filing_operations.prepare_learning_intake(ctx, args, uow, metadata=metadata, source_refs=source_refs)
