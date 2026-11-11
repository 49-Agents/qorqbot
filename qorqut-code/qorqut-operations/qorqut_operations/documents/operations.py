"""Operational source documents: notes, the document ledger (topics, notes, people), source reads and their plans.

This is the source half of the former mixed document service. It owns source records, metadata and bytes, the Project,
Global and agent scopes, journal visibility, attribution, archive behavior, the file-index inventory and target validation.
It does not own Context filing, the published Context reader, plans or working memory: those are host workflows that
call the generic helpers here (``builder``, ``add_indexes``, ``finalize``, ``result``, ``edit_document``...) with this one
service and its store passed explicitly, so no source mutation is written twice.

Operational source kinds named ``context_*`` remain operational here: they write registered documents only. The published
Context graph changes solely through ``context_edit`` and the filing queue, never through these operations.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from dataclasses import replace
from pathlib import PurePosixPath
from uuid import UUID as UUIDValue, uuid5

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import (DATE, IDENTITY, PAGE, TEXT, UUID, PlanBuilder, apply_records, page,
                                                 ref, require_revision, rows, schema, utc_clock)
from qorqut_operations.contracts import (DomainError, ExpectedTarget, ResourceRef, canonical_hash, plain)
from qorqut_operations.documents.serialization import BODY, HASH, SLUG, slugify, split_document
from qorqut_operations.records.associations import TASK_IDS, link_new_resource
from qorqut_operations.records.vocabulary import document_type
from qorqut_operations.relationships import create_edge
from . import business_notes
from .business_notes import KIND as BUSINESS_NOTE, TARGET, require_target
from .evidence import MATERIAL_TYPES
from .journal_access import JOURNAL_KINDS, entries as journal_entries, require_owner
from .library import READ_ARGUMENTS, SourceLibrary
from .materials import TAG, TAGS, classification, matches
from .people import AFFILIATION, CONTACT, affiliation_effect, details as person_details, validate_changes as validate_person_changes
from .relationships import replace_targets

NOTE_META = schema({"title": TEXT, "date": DATE, "pinned": {"type": "boolean"}, "tags": TAGS, "ordubashy_visible": {"type": "boolean"}, "statuses": {"type": "object", "maxProperties": 2000,
    "propertyNames": {"maxLength": 200000}, "additionalProperties": {"enum": ["todo", "white", "in_progress", "done", "blocked", "cancelled"]}}})
PERSON = {"name": TEXT, "aka": {"type": "array", "maxItems": 50, "items": TEXT, "uniqueItems": True},
          "role": TEXT, "org": TEXT, "about": BODY, "notes": BODY, "summary": BODY,
          "affiliation": AFFILIATION, "contact": CONTACT}
PEOPLE_IDS = {"type": "array", "items": UUID, "maxItems": 50, "uniqueItems": True}
CONTEXT_NOTE = {"topic_id": UUID, "title": TEXT, "body": BODY, "people_ids": PEOPLE_IDS,
                "tags": TAGS, "material_type": {"enum": list(MATERIAL_TYPES)}, "summary": BODY,
                "part": {"type": "string", "pattern": "^[1-9][0-9]*/[1-9][0-9]*$"}, "also_topic_ids": PEOPLE_IDS}
TOPIC = {"parent_id": UUID, "title": TEXT, "slug": SLUG, "about": BODY}
# The one registered Context tree: every topic, note and generated index lives below it.
CONTEXT_TREE = "context"
TOPIC_DEPTH = 4

# Notes the installation owner may keep at Global (the journal) and the owner-only Global note rules that guard them.
GLOBAL_NOTE_OPS = frozenset({"notes_list", "notes_get", "notes_create", "notes_update", "notes_delete"})
# Context operations the installation owner may also use at Global, under `read_global_context` (no new capability).
GLOBAL_OWNER_CONTEXT = frozenset({"context_upload", "context_people_list", "context_people_get"})
# The document-ledger authoring operations (People, topics, notes, document updates). They write registered documents only; the
# published Context graph is changed solely by ``context_edit`` and the filing queue, never by these.
DOCUMENT_AUTHORING = frozenset({
    "context_people_create", "context_people_update", "context_people_merge", "context_people_affiliation_set",
    "context_topics_create", "context_topics_move", "context_notes_create", "context_notes_move", "context_document_update",
})
NOTE_KINDS = {"business_note", "agent_note", "journal", "global_note", BUSINESS_NOTE}
CONTEXT_KINDS = {"context_topic", "context_note", "context_person", "context_upload", "context_dump", "context_map", "context", "pillar", "email_message", "content"}
DOCUMENT_UPDATE_KINDS = {"context", "pillar", "context_note", "context_topic", "context_person", "context_map", "context_upload", "context_dump", "content"}

READS = {
    "notes_list": {**PAGE, "agent_id": UUID, TARGET: UUID, "tag": TAG}, "notes_get": {"document_id": UUID},
    "content_list": {**PAGE, "kind": TEXT, "query": TEXT}, "content_get": {"document_id": UUID},
    "context_list": READ_ARGUMENTS["context_list"], "context_get": READ_ARGUMENTS["context_get"],
    "context_search": READ_ARGUMENTS["context_search"],
    "context_people_list": {**PAGE, "query": TEXT}, "context_people_get": {"document_id": IDENTITY},
    "context_maps_get": {**PAGE, "upload_id": UUID},
}
MUTATIONS = {
    "notes_create": ({"title": TEXT, "body": BODY, "date": DATE, "pinned": {"type": "boolean"}, "tags": TAGS, "target_agent_id": UUID, "task_ids": TASK_IDS,
        TARGET: UUID}, ("title", "body")),
    "notes_update": ({"document_id": UUID, "body": BODY, "metadata": NOTE_META, "expected_content_hash": HASH, TARGET: UUID}, ("document_id",)),
    "notes_delete": ({"document_id": UUID, "expected_content_hash": HASH, TARGET: UUID}, ("document_id",)),
    "context_document_update": ({"document_id": UUID, "body": BODY, "expected_content_hash": HASH}, ("document_id", "body")),
    "context_topics_create": (TOPIC, ("title", "slug")),
    "context_topics_move": ({"document_id": UUID, "parent_id": UUID, "slug": SLUG}, ("document_id", "slug")),
    "context_notes_create": (CONTEXT_NOTE, ("topic_id", "title", "body")),
    "context_notes_move": ({"document_id": UUID, "topic_id": UUID, "expected_content_hash": HASH}, ("document_id", "topic_id")),
    "context_people_create": (PERSON, ("name",)),
    "context_people_update": ({"document_id": UUID, "changes": schema(PERSON), "expected_content_hash": HASH}, ("document_id", "changes")),
    "context_people_affiliation_set": ({"document_id": UUID, "affiliation": AFFILIATION}, ("document_id", "affiliation")),
    "context_people_merge": ({"document_id": UUID, "merge_id": UUID, "merge_revision": {"type": "integer", "minimum": 1}}, ("document_id", "merge_id", "merge_revision")),
}
DESCRIPTIONS = {
    "context_list": instruction_text('operations-browse-scoped-context-metadata-with-pagination-without-loading'),
    "context_search": instruction_text('operations-search-context-with-bounded-excerpts-check-complete-and'),
    "context_get": instruction_text('operations-read-a-context-document-by-id-heading-or'),
    "notes_list": instruction_text('operations-list-your-own-notes-with-target-business-id'),
    "notes_delete": instruction_text('operations-archive-a-note-while-preserving-its-contents-and'),
    "notes_create": instruction_text('operations-create-your-private-note-with-target-business-id'),
    "notes_update": instruction_text('operations-edit-a-note-with-expected-revision-a-project'),
    "context_people_affiliation_set": instruction_text('operations-set-a-person-internal-external-classification-using-current'),
    "context_people_update": instruction_text('operations-edit-person-details-including-contact-email-phone-notes'),
}


def append_plan(builder, plan, operation):
    for target in plan.expected_targets:
        if target.resource in builder.targets and builder.targets[target.resource] != target:
            raise DomainError("invalid_plan", "Compound operation snapshots disagree")
        builder.targets[target.resource] = target
    builder.changes.extend(replace(change, operation_name=change.operation_name or operation) for change in plan.changes)
    builder.previews.extend(plan.preview["changes"])
    builder.resources.extend(plan.preview["resources"])
    builder.effects.extend(plan.preview["effects"])


def business_namespace(ctx, args):
    return "business:" + ctx.business_id if ctx.business_id else "global"


def own_namespace(ctx, args):
    """The namespace of an agent's own documents (a selected Project note target wins), or the selected scope."""
    if args.get(TARGET):
        return "business:" + args[TARGET]
    return "agent:" + ctx.agent_id if ctx.agent_id and ctx.home_business_id is None else "business:" + ctx.business_id if ctx.business_id else "global"


def identity_view(view, row, ctx, uow):
    """The presentation of a source-only caller: the view exactly as the store composed it."""
    return view


class DocumentSources:
    """The one source service: store, library and the operational source operations.

    ``agent_workspace(agent_id, business=None)`` and ``scope_root(business=None)`` are the host's existing location functions
    bound to its paths; they place an agent's own note files. ``present(view, row, ctx, uow)`` is the publication adapter's
    composition of one unscoped read (identity for a source-only caller). ``item_resolver`` is the host's optional record
    resolver of a new resource's Task associations."""

    def __init__(self, store, library, *, agent_workspace, scope_root, clock=utc_clock, present=identity_view,
                 item_resolver=None):
        for name, value in (("agent_workspace", agent_workspace), ("scope_root", scope_root), ("present", present)):
            if not callable(value):
                raise TypeError(f"The document sources need a callable {name}")
        if not isinstance(library, SourceLibrary) or library.store is not store:
            raise TypeError("The document sources read through the library over their own store")
        self.store, self.library, self.clock = store, library, clock
        self._agent_workspace, self._scope_root, self.present = agent_workspace, scope_root, present
        self.item_resolver = item_resolver
        self.descriptors = self._descriptors()

    # -- registration ----------------------------------------------------------------------------------------------------

    def register_targets(self, application):
        application.register_target_resolver("managed_document_files", self.store.resolve_snapshot)
        application.register_target_resolver("document_index_sources", self.index_snapshot)

    def register(self, registry, application):
        self.register_targets(application)
        # A trusted port: the library's bounded single-document read for composed callers that hold a write
        # transaction (Action evidence), which the registered ``context_get`` read is too slow for.
        application.document_library = self.library
        for descriptor in self.descriptors.values():
            registry.register(descriptor)

    def _descriptors(self):
        found = {}
        for name, properties in READS.items():
            library = name in READ_ARGUMENTS
            shared = library or name in GLOBAL_OWNER_CONTEXT
            agent_notes = name.startswith("notes_")
            required = ("document_id",) if "document_id" in properties else (("query",) if name == "context_search" else ())
            found[name] = OperationDescriptor(name, schema(properties, required), effect_kind="read", supports_signoff=False,
                scope_kinds={"business", "agent_workspace", "global"} if name in GLOBAL_NOTE_OPS else {"business", "global"} if shared else {"business"},
                resolve_namespace=business_namespace if shared else own_namespace if agent_notes else None,
                global_capability="read_global_context" if shared or name in GLOBAL_NOTE_OPS else None,
                exposure=frozenset({"dashboard", "worker"}) if name.startswith("context_") and not library else frozenset({"mcp", "dashboard", "worker"}),
                description=DESCRIPTIONS.get(name, ""),
                owner_session=name in {"context_people_list", "context_people_get", "context_maps_get"},
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow))
        for name, (properties, required) in MUTATIONS.items():
            found[name] = self.mutation_descriptor(name, properties, required, prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply, validate_targets=self.validate_targets,
                scope_kinds={"business", "agent_workspace", "global"} if name in GLOBAL_NOTE_OPS else {"business"},
                resolve_namespace=own_namespace if name in GLOBAL_NOTE_OPS else None,
                global_capability="read_global_context" if name in GLOBAL_NOTE_OPS else None,
                exposure={"dashboard", "worker"} if name in DOCUMENT_AUTHORING else {"mcp", "dashboard", "worker"})
        return found

    @staticmethod
    def mutation_descriptor(name, properties, required, *, prepare, apply, validate_targets, scope_kinds, resolve_namespace,
                            global_capability, exposure, owner_only=False, description=None):
        """The one compound document-mutation descriptor shape (the rationale argument, no Signoff exemption)."""
        return OperationDescriptor(name, schema({**properties, "rationale": TEXT}, required), effect_kind="compound",
            description=DESCRIPTIONS.get(name, "") if description is None else description,
            scope_kinds=scope_kinds, resolve_namespace=resolve_namespace, global_capability=global_capability,
            exposure=exposure, owner_only=owner_only, prepare=prepare, apply=apply, validate_targets=validate_targets)

    # -- shared source helpers -------------------------------------------------------------------------------------------

    @staticmethod
    def own_namespace(ctx, args):
        return own_namespace(ctx, args)

    def active_agent(self, ctx, uow, identity=None, *, write=False):
        """The active teammate profile a document operation selects (the caller's own by default)."""
        identity = identity or ctx.agent_id
        if identity is None:
            raise DomainError("validation_error", "An agent selector is required for a plan")
        profile = uow.repository("agent_profiles", business_id=ctx.business_id).get(identity)
        if profile is None and identity == ctx.agent_id:
            profile = uow.repository("agent_profiles", business_id=None).get(identity)
        if profile is None or profile["status"] != "active":
            raise DomainError("not_found", "Active teammate not found")
        if write and ctx.principal_kind == "agent" and identity != ctx.agent_id:
            raise DomainError("forbidden", "Agents can edit only their own documents")
        return profile

    def agent_prefix(self, profile, uow):
        """The registered agent's identity directory in its scope root, relative to that root; never its name."""
        business_id = profile["home_business_id"]
        business = None if business_id is None else uow.repository("businesses").get(business_id, required=True)
        workspace = self._agent_workspace(profile["agent_id"], business)
        return str(workspace.relative_to(self._scope_root(business))), business_id

    @staticmethod
    def note_attribution(ctx, uow, business_id):
        """Validate a selected business used only as a private-note tag."""
        if business_id is None:
            return None
        if business_id not in ctx.allowed_business_ids:
            raise DomainError("forbidden", "Note attribution is outside the principal's project scope")
        business = uow.repository("businesses").get(business_id)
        if business is None or business.get("archived_at") is not None:
            raise DomainError("forbidden", "Note attribution requires an active project")
        return {"related_business_id": business["id"]}

    @staticmethod
    def check_global_notes(operation, ctx, args, uow, operations=GLOBAL_NOTE_OPS):
        if operation not in operations or ctx.business_id is not None or ctx.agent_id:
            return
        owner = uow.repository("installation").get("service")
        if not owner or ctx.principal_kind != "human" or owner["owner_principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "Global notes belong to the installation owner")
        if args.get("agent_id") or args.get("target_agent_id") or args.get("task_ids"):
            raise DomainError("validation_error", "Global notes do not have project or agent associations")

    @staticmethod
    def note_target(ctx, args, row):
        if row["kind"] == BUSINESS_NOTE:
            require_target(ctx, args, row["business_id"])
        elif TARGET in args:
            raise DomainError("validation_error", "target_business_id applies only to business-owned notes")

    def builder(self, operation, ctx, args, *, namespace=None):
        """The plan builder of one document operation. The namespace follows the operation's scope unless the caller (a host
        workflow with its own scope rule) names it."""
        if namespace is None:
            namespace = ("global" if ctx.business_id is None and operation.startswith("context_")
                         else own_namespace(ctx, args) if operation.startswith("notes_") else "business:" + ctx.business_id)
        scope = {"kind": "global"} if namespace == "global" else ({"kind": "agent_workspace", "agent_id": ctx.agent_id} if namespace.startswith("agent:") else {"kind": "business", "business_id": ctx.business_id})
        return PlanBuilder(ctx, operation, args, operation.replace("_", " ").title(), clock=self.clock, namespace=namespace, affected_scope=scope)

    def finalize(self, builder, uow, operation, args):
        """Associate a Project document created by this plan with its Tasks and maintain the flat file inventory."""
        ctx = builder.ctx
        if ctx.business_id is not None and operation in {"notes_create", "notes_upload", "context_upload"}:
            for change in tuple(builder.changes):
                if change.resource.resource_type == "documents" and change.action == "insert":
                    row = change.values
                    if change.resource.business_id != ctx.business_id and args.get("task_ids"):
                        raise DomainError("forbidden", "Tasks relate documents in their own project")
                    if row["kind"] not in {"business_note", "agent_note", "context_upload", BUSINESS_NOTE}:
                        continue
                    link_new_resource(builder, uow, "documents", change.resource.resource_id, args.get("task_ids", ()),
                        inherit_from=args.get("document_id") if operation == "notes_upload" else None,
                        item_resolver=self.item_resolver)
        self.add_indexes(builder, uow)
        return builder.build()

    def edit_document(self, builder, uow, row, args, options, *, namespace):
        """One registered document edit or archive: the single implementation behind note, document and instruction updates."""
        ctx = builder.ctx
        if builder.operation.endswith("delete"):
            self.store.archive(builder, uow, row, expected_revision=options.expected_revision,
                              expected_content_hash=args.get("expected_content_hash"), namespace=namespace)
            return
        edits = args.get("changes", args.get("metadata", {}))
        if "ordubashy_visible" in edits and row["kind"] not in JOURNAL_KINDS:
            raise DomainError("validation_error", "Visibility belongs to owner journal entries only")
        original = self.store.read_bytes(row, uow)
        metadata, old_body = split_document(original)
        metadata = {**row["metadata_json"], **metadata}
        migrating_journal = row["kind"] == "journal" and not edits and "body" not in args
        if not migrating_journal:
            metadata.update({key: value for key, value in edits.items() if key != "body"}, updated=builder.at)
        if args.get("body", edits.get("body", old_body)) != old_body: metadata["content_owner"] = ctx.principal_id
        if "summary" in edits: metadata["summary_owner"] = ctx.principal_id
        if row["kind"] == BUSINESS_NOTE:
            # The author is the registered attribution, never edited frontmatter.
            metadata["author_id"] = row["metadata_json"].get("author_id")
        self.store.write(builder, uow, kind="business_note" if row["kind"] == "journal" else row["kind"], before=row, body=args.get("body", edits.get("body", old_body)),
            metadata=metadata, data=original if migrating_journal else None,
            business_id=row["business_id"], namespace=namespace,
            expected_revision=options.expected_revision, expected_content_hash=args.get("expected_content_hash"))

    # -- reads -----------------------------------------------------------------------------------------------------------

    def read(self, operation, ctx, args, uow):
        self.check_global_notes(operation, ctx, args, uow)
        if operation in READ_ARGUMENTS and (ctx.source == "mcp" or 'scope' in args):
            return self.library.read(operation, ctx, args, uow)
        if "document_id" in args:
            kinds = {"notes_get": NOTE_KINDS,
                "content_get": {"content", "context_note"}, "context_people_get": {"context_person"}}.get(operation)
            row = self.store.get(ctx, args["document_id"], uow, kinds=kinds)
            if operation in GLOBAL_NOTE_OPS and (row["kind"] == "global_note" or ctx.business_id is None and not ctx.agent_id):
                if (row["kind"] not in JOURNAL_KINDS | {"agent_note"} or ctx.agent_id
                        or (row["kind"] == "global_note" and ctx.business_id is not None)):
                    raise DomainError("not_found", "Note is outside the selected scope")
            if operation.startswith("context_") and row["kind"] not in CONTEXT_KINDS:
                raise DomainError("validation_error", "Not a registered context document")
            return {"document": self.present(self.store.view(row, uow), row, ctx, uow)}
        kinds = {"notes_list": {"agent_note", "business_note", "journal", "global_note"},
            "content_list": {"content", "context_note"}, "context_people_list": {"context_person"},
            "context_maps_get": {"context_map"}}.get(operation, CONTEXT_KINDS)
        project_notes = operation == "notes_list" and TARGET in args
        if operation == "notes_list" and not project_notes and ctx.business_id and not ctx.agent_id and not args.get("agent_id"):
            raise DomainError("validation_error", "Project notes require target_business_id")
        if project_notes:
            found = self._project_notes(ctx, args, uow)
        elif operation == "notes_list" and ctx.business_id is None and not ctx.agent_id and not args.get("agent_id"):
            require_owner(ctx, uow)
            found = journal_entries(uow)
        else:
            found = self.store.collection(ctx, uow, kinds=kinds)
        if operation != "notes_list":
            found = [row for row in found if matches(classification(row), args)]
        if operation == "notes_list" and not project_notes:
            agent = args.get("agent_id", ctx.agent_id)
            if agent:
                profile = self.active_agent(ctx, uow, agent)
                if profile["home_business_id"] != ctx.business_id:
                    found = rows(uow, "documents", profile["home_business_id"])
                    found = [row for row in found if row["kind"] == "agent_note"]
                    # Global agents keep private notes in their Global home. A
                    # selected business is an attribution filter, not a storage
                    # or access scope for those notes.
                    if ctx.business_id is not None and profile["home_business_id"] is None:
                        found = [row for row in found
                                 if row["metadata_json"].get("related_business_id") == ctx.business_id]
                found = [row for row in found if row["owning_agent_id"] == agent]
            if ctx.business_id is None and not agent:
                # The owner's journal lists personal notes, not agent working notes.
                found = [row for row in found if row["kind"] in JOURNAL_KINDS]
        if operation == "notes_list" and "tag" in args:
            found = [row for row in found if args["tag"] in row["metadata_json"].get("tags", [])]
        if args.get("topic_id"):
            topic = self.store.get(ctx, args["topic_id"], uow, kinds={"context_topic"})
            found = [row for row in found if topic["id"] in row["metadata_json"].get("topic_ids", [])]
        for key in ("type", "applies", "kind", "pinned", "upload_id"):
            if key in args:
                found = [row for row in found if (row["kind"] if key == "kind" and operation == "context_list" else row["metadata_json"].get(key)) == args[key]]
        if args.get("query"):
            needle = args["query"].casefold()
            found = [row for row in found if needle in (str(row["metadata_json"]) + " " + self.store.view(row, uow)["body"]).casefold()]
        selected, cursor = page(found, args, ctx.business_id, operation)
        return {"items": [self.present(self.store.view(row, uow), row, ctx, uow) for row in selected], "next_cursor": cursor}

    def _project_notes(self, ctx, args, uow):
        """The selected project's own notes; private and related-business notes never qualify."""
        if args.get("agent_id"):
            raise DomainError("validation_error", "target_business_id lists project notes; omit agent_id")
        if ctx.business_id is None:
            raise DomainError("scope_required", "Select the project whose notes to list")
        if args[TARGET] != ctx.business_id:
            raise DomainError("forbidden", "target_business_id differs from the selected project")
        return self.store.collection(ctx, uow, kinds={BUSINESS_NOTE})

    # -- plans -----------------------------------------------------------------------------------------------------------

    def _create_business_note(self, builder, uow, args):
        ctx = builder.ctx
        business_id = require_target(ctx, args, ctx.business_id)
        if args.get("target_agent_id"):
            raise DomainError("validation_error", "Business-owned notes have no agent owner")
        metadata = {"title": args["title"], "date": args.get("date", builder.at[:10]),
                    "author_id": ctx.principal_id, "pinned": bool(args.get("pinned", False))}
        if "tags" in args:
            metadata["tags"] = list(args["tags"])
        path = self.store.free_path(uow, business_id, "notes", builder.at[:10] + "-" + slugify(args["title"]), builder=builder)
        self.store.write(builder, uow, kind=BUSINESS_NOTE, path=path, body=args["body"], metadata=metadata, business_id=business_id)

    def _topic(self, builder, uow, args, *, operation_name=None):
        tree = CONTEXT_TREE
        parent = self.store.get(builder.ctx, args["parent_id"], uow, kinds={"context_topic"}) if args.get("parent_id") else None
        if parent:
            builder.expect(ref("documents", parent["id"], builder.ctx.business_id), parent)
        prefix = str(PurePosixPath(parent["path"]).parent) if parent else tree
        path = str(PurePosixPath(prefix) / args["slug"] / "_topic.md")
        if len(PurePosixPath(path).parts) - 2 > TOPIC_DEPTH:
            raise DomainError("validation_error", "Topic exceeds the existing tree depth limit")
        if any(row["path"] == path for row in self.store.collection(builder.ctx, uow)):
            raise DomainError("conflict", "Topic already exists")
        identity = self.store.write(builder, uow, kind="context_topic", path=path,
            body="# " + args["title"] + "\n\n" + args.get("about", "") + "\n\n## Summary\n",
            metadata={"title": args["title"], "tree": tree, "topic": str(PurePosixPath(path).parent.relative_to(tree)), "about": args.get("about", "")}, operation_name=operation_name)
        if parent:
            create_edge(builder, {"type": "topic", "id": identity}, {"type": "topic", "id": parent["id"]}, "part_of", operation_name=operation_name)
        return identity

    def _person(self, builder, uow, args, *, operation_name=None):
        validate_person_changes(builder.ctx, args)
        name = args["name"].strip()
        if not name: raise DomainError("validation_error", "Person name cannot be blank")
        metadata = {key: value for key, value in args.items() if key in PERSON}
        path = self.store.free_path(uow, builder.ctx.business_id, "context/_people", slugify(name), builder=builder)
        metadata.update(person_slug=PurePosixPath(path).stem, title=name, tree="context")
        metadata.setdefault("affiliation", "external")
        if "summary" in args: metadata["summary_owner"] = builder.ctx.principal_id
        return self.store.write(builder, uow, kind="context_person", path=path, metadata=metadata, body=args.get("notes", ""), operation_name=operation_name)

    def _note(self, builder, uow, args, *, topic=None, people=None, prepared_people=(), source_refs=None, operation_name=None, kind="context_note"):
        topic = topic or self.store.get(builder.ctx, args["topic_id"], uow, kinds={"context_topic"})
        builder.expect(ref("documents", topic["id"], builder.ctx.business_id), topic) if topic.get("revision") else None
        people = people if people is not None else args.get("people_ids", [])
        for identity in people:
            row = self.store.get(builder.ctx, identity, uow, kinds={"context_person"})
            builder.expect(ref("documents", identity, builder.ctx.business_id), row)
        if len(args.get("summary", "").split()) > 30: raise DomainError("validation_error", "Context note summaries are limited to 30 words")
        if args.get("part"):
            numerator, denominator = map(int, args["part"].split("/"))
            if numerator > denominator: raise DomainError("validation_error", "Note part cannot exceed its total")
        for also_id in args.get("also_topic_ids", []):
            also = self.store.get(builder.ctx, also_id, uow, kinds={"context_topic"})
            builder.expect(ref("documents", also_id, builder.ctx.business_id), also)
        metadata = {key: value for key, value in args.items() if key in {"title", "tags", "summary", "part", "also_topic_ids", "kind", "url", "material_type", "source_spans", "origin", "occurred_on"}}
        metadata.update(tree=CONTEXT_TREE, topic_id=topic["id"], people=[*people, *prepared_people],
                        date=builder.at[:10], by=builder.ctx.principal_id)
        path = self.store.free_path(uow, builder.ctx.business_id, str(PurePosixPath(topic["path"]).parent), builder.at[:10] + "-" + slugify(args["title"]), builder=builder)
        body = args["body"]
        origins = list(source_refs or [])
        identity = self.store.write(builder, uow, kind=kind, path=path, body=body, metadata=metadata, source_refs=origins,
            operation_name=operation_name)
        source = {"type": "material", "id": identity}
        create_edge(builder, source, {"type": "topic", "id": topic["id"]}, "part_of", operation_name=operation_name)
        for person_id in dict.fromkeys([*people, *prepared_people]):
            create_edge(builder, source, {"type": "person", "id": person_id}, "mentions", operation_name=operation_name)
        for topic_id in args.get("also_topic_ids", []):
            create_edge(builder, source, {"type": "topic", "id": topic_id}, "concerns", operation_name=operation_name)
        for origin in source_refs or []:
            if origin["resource_type"] == "documents":
                original = self.store.get(builder.ctx, origin["resource_id"], uow)
                create_edge(builder, source, {"type": document_type(original), "id": original["id"]}, "extracted_from", operation_name=operation_name)
            elif origin["resource_type"] in {"signoffs", "tasks", "action_logs"}:
                resource = origin["resource_type"]
                original = uow.repository(resource, business_id=builder.ctx.business_id).get(origin["resource_id"], required=True)
                builder.expect(ref(resource, original["id"], builder.ctx.business_id), original)
                create_edge(builder, source, {"type": {"signoffs": "signoff", "tasks": "task", "action_logs": "action"}[resource], "id": original["id"]}, "extracted_from", operation_name=operation_name)
        return identity

    def prepare(self, operation, ctx, args, options, uow):
        self.check_global_notes(operation, ctx, args, uow)
        builder = self.builder(operation, ctx, args)
        if operation == "context_people_affiliation_set":
            args = {"document_id": args["document_id"], "changes": {"affiliation": args["affiliation"]}}
            operation = "context_people_update"
        if operation == "notes_create" and TARGET in args:
            self._create_business_note(builder, uow, args)
        elif operation == "notes_create":
            metadata = {key: value for key, value in args.items() if key not in {"body", "rationale", "target_agent_id", "task_ids"}}
            metadata.update(created=builder.at, updated=builder.at, by=ctx.principal_id)
            selected_business_id = ctx.business_id
            business_id, agent_id, namespace = selected_business_id, None, None
            if args.get("target_agent_id") and (ctx.source != "worker" or ctx.principal_kind != "system" or not ctx.job_id):
                raise DomainError("forbidden", "Targeted agent notes require a claimed background job")
            if ctx.agent_id or args.get("target_agent_id"):
                profile = self.active_agent(ctx, uow, args.get("target_agent_id"), write=True)
                prefix, business_id = self.agent_prefix(profile, uow)
                prefix, kind, agent_id = prefix + "/files/notes", "agent_note", profile["agent_id"]
                namespace = builder.namespace if business_id is None else None
                if business_id is None and selected_business_id is not None:
                    metadata.update(self.note_attribution(ctx, uow, selected_business_id))
            else:
                require_owner(ctx, uow)
                if ctx.business_id is not None:
                    raise DomainError("scope_required", "Create personal entries in the Global journal")
                prefix, kind, namespace = "notes", "global_note", "global"
                metadata["ordubashy_visible"] = True
            if kind in {"business_note", "global_note"}: metadata["date"] = args.get("date", builder.at[:10])
            path = self.store.free_path(uow, business_id, prefix, builder.at[:10] + "-" + slugify(args["title"]), builder=builder)
            self.store.write(builder, uow, kind=kind, path=path, body=args["body"], metadata=metadata,
                             agent_id=agent_id, business_id=business_id, namespace=namespace)
        elif operation in {"notes_update", "notes_delete", "context_document_update"}:
            kinds = {"notes_update": NOTE_KINDS, "notes_delete": NOTE_KINDS,
                "context_document_update": DOCUMENT_UPDATE_KINDS}[operation]
            row = self.store.get(ctx, args["document_id"], uow, kinds=kinds, write=True)
            self.note_target(ctx, args, row)
            if operation in GLOBAL_NOTE_OPS and (row["kind"] == "global_note" or ctx.business_id is None and not ctx.agent_id):
                if (row["kind"] not in JOURNAL_KINDS | {"agent_note"} or ctx.agent_id
                        or (row["kind"] == "global_note" and ctx.business_id is not None)):
                    raise DomainError("not_found", "Note is outside the selected scope")
            if operation == "context_document_update" and row["business_id"] != ctx.business_id:
                raise DomainError("forbidden", "Shared project context must belong to the selected project")
            namespace = builder.namespace if row["business_id"] is None else None
            self.edit_document(builder, uow, row, args, options, namespace=namespace)
        elif operation == "context_topics_create":
            self._topic(builder, uow, args)
        elif operation == "context_topics_move":
            self._move_topic(builder, uow, args, options)
        elif operation == "context_notes_create":
            self._note(builder, uow, args)
        elif operation == "context_notes_move":
            row = self.store.get(ctx, args["document_id"], uow, kinds={"context_note"})
            topic = self.store.get(ctx, args["topic_id"], uow, kinds={"context_topic"})
            builder.expect(ref("documents", topic["id"], ctx.business_id), topic)
            path = self.store.free_path(uow, ctx.business_id, str(PurePosixPath(topic["path"]).parent), PurePosixPath(row["path"]).stem, builder=builder)
            self.store.write(builder, uow, kind=row["kind"], before=row, path=path,
                metadata={**row["metadata_json"], "topic_id": topic["id"]}, expected_revision=options.expected_revision, expected_content_hash=args.get("expected_content_hash"))
            replace_targets(builder, uow, {"type": "material", "id": row["id"]}, "part_of", "topic", [topic["id"]])
        elif operation == "context_people_create":
            self._person(builder, uow, args)
        elif operation == "context_people_update":
            validate_person_changes(ctx, args["changes"])
            principal = uow.repository("principals").get(args["document_id"])
            member = uow.repository("business_memberships", business_id=ctx.business_id).get(args["document_id"]) if principal else None
            if principal and principal["kind"] == "human" and member and member["active"]:
                if set(args["changes"]) - {"affiliation", "contact", "summary"}:
                    raise DomainError("validation_error", "Human account details support affiliation, contact and summary only")
                require_revision(member, options.expected_revision)
                values = {**member.get("person_json", {}), **args["changes"]}
                if "contact" in args["changes"]:
                    values["contact"] = {**person_details(principal, uow, business_id=ctx.business_id)["contact"], **args["changes"]["contact"]}
                resource = builder.record("business_memberships", principal["id"], member, {"person_json": values}, label=principal["display_name"])
                if "affiliation" in args["changes"]:
                    affiliation_effect(builder, resource)
                return builder.build()
            row = self.store.get(ctx, args["document_id"], uow, kinds={"context_person"})
            metadata = {**row["metadata_json"], **args["changes"]}
            if "contact" in args["changes"]:
                metadata["contact"] = {**person_details(row, uow)["contact"], **args["changes"]["contact"]}
            if "notes" in args["changes"]: metadata["content_owner"] = ctx.principal_id
            if "summary" in args["changes"]: metadata["summary_owner"] = ctx.principal_id
            self.store.write(builder, uow, kind=row["kind"], before=row, metadata=metadata, body=args["changes"].get("notes"),
                             expected_revision=options.expected_revision, expected_content_hash=args.get("expected_content_hash"))
        elif operation == "context_people_merge":
            self._merge_people(builder, uow, args, options)
        else:
            raise DomainError("operation_unavailable", "Document operation is not implemented")
        return self.finalize(builder, uow, operation, args)

    @staticmethod
    def index_snapshot(ctx, resource, uow):
        if resource.resource_id != CONTEXT_TREE:
            raise DomainError("invalid_plan", "Unknown context index tree")
        tree = CONTEXT_TREE
        sources = [row for row in rows(uow, "documents", resource.business_id)
                   if row["path"].startswith(tree + "/") and row["path"] != tree + "/INDEX.md"]
        return {"content_hash": canonical_hash(sorted((row["id"], row["revision"]) for row in sources))}

    def add_indexes(self, builder, uow):
        """Maintain a flat file inventory, not a second relationship hierarchy."""
        changed = [change for change in builder.changes if change.resource.resource_type == "documents"
                   and change.resource.business_id is not None]
        if not changed: return
        current = {row["id"]: row for row in rows(uow, "documents", builder.ctx.business_id)}
        projected = {identity: plain(row) for identity, row in current.items()}
        touched = False
        for change in changed:
            before = current.get(change.resource.resource_id, {})
            for path in (before.get("path", ""), change.values.get("path", "")):
                if path.split("/")[0] == CONTEXT_TREE and path != CONTEXT_TREE + "/INDEX.md": touched = True
            if change.action == "delete": projected.pop(change.resource.resource_id, None)
            else: projected[change.resource.resource_id] = {**before, **plain(change.values)}
        if touched:
            tree = CONTEXT_TREE
            source = ResourceRef("document_index_sources", tree, builder.ctx.business_id)
            builder.targets[source] = ExpectedTarget(source, content_hash=self.index_snapshot(builder.ctx, source, uow)["content_hash"])
            relevant = [row for row in projected.values() if not row.get("archived") and row.get("path", "").startswith(tree + "/") and row["path"] != tree + "/INDEX.md"]
            topics = sorted((row for row in relevant if row.get("kind") == "context_topic"), key=lambda row: row["path"])
            lines = ["# " + tree.replace("-", " ").capitalize() + " Index", "", "File inventory. Query relationships for logical membership and hierarchy.", ""]
            pillars = sorted(row["path"] for row in relevant if row.get("kind") in {"context", "pillar"})
            if pillars: lines += ["## Pillars", "", *["- [" + PurePosixPath(path).name + "](" + str(PurePosixPath(path).relative_to(tree)) + ")" for path in pillars], ""]
            lines += ["## Topics", ""]
            for topic in topics:
                directory = PurePosixPath(topic["path"]).parent
                path = str(directory.relative_to(tree))
                metadata = topic.get("metadata_json", {})
                about = " ".join(str(metadata.get("about", "")).split())[:160]
                title = " ".join(str(metadata.get("title", directory.name)).split())
                lines.append(f"- [{title}]({path}/_topic.md): {about}")
            if not topics: lines.append("- No topics")
            people = sorted((row for row in relevant if row.get("kind") == "context_person"), key=lambda row: row["path"])
            if people: lines += ["", "## People", ""]
            for person in people:
                metadata = person.get("metadata_json", {})
                name = " ".join(str(metadata.get("name", PurePosixPath(person["path"]).stem)).split())
                lines.append(f"- [{name}]({PurePosixPath(person['path']).relative_to(tree)})")
            pending = sum(row.get("kind") == "context_upload" for row in relevant)
            lines += ["", f"## Inbox: {pending} uploads waiting", ""]
            body = "\n".join(lines).rstrip() + "\n"
            existing = next((row for row in current.values() if row["path"] == tree + "/INDEX.md"), None)
            if existing and existing["kind"] != "context_index":
                raise DomainError("invalid_configuration", "Context index must be registered as a generated context_index document")
            identity = existing["id"] if existing else str(uuid5(UUIDValue(builder.ctx.business_id), "document:" + tree + "/INDEX.md"))
            self.store.write(builder, uow, kind="context_index", before=existing, identity=identity, path=tree + "/INDEX.md", body=body,
                metadata={"title": tree + " index", "tree": tree, "generated": True}, expected_revision=existing["revision"] if existing else None)

    def _move_topic(self, builder, uow, args, options):
        row = self.store.get(builder.ctx, args["document_id"], uow, kinds={"context_topic"})
        require_revision(row, options.expected_revision)
        tree = CONTEXT_TREE
        old = PurePosixPath(row["path"]).parent
        parent = self.store.get(builder.ctx, args["parent_id"], uow, kinds={"context_topic"}) if args.get("parent_id") else None
        if parent:
            builder.expect(ref("documents", parent["id"], builder.ctx.business_id), parent)
        target = (PurePosixPath(parent["path"]).parent if parent else PurePosixPath(tree)) / args["slug"]
        if target == old or target.is_relative_to(old):
            raise DomainError("validation_error", "Topic cannot move into itself or a descendant")
        found = [item for item in self.store.collection(builder.ctx, uow) if PurePosixPath(item["path"]).is_relative_to(old)]
        if len(found) > 250: raise DomainError("result_too_large", "Topic move exceeds 250 registered files")
        for item in found:
            if item["kind"] not in {"context_topic", "context_note", "content"}:
                raise DomainError("forbidden", "Topic moves cannot relocate protected instructions or unrelated authored files")
            path = target / PurePosixPath(item["path"]).relative_to(old)
            depth = len(path.parts) - 2
            if depth > TOPIC_DEPTH: raise DomainError("validation_error", "Moved topic exceeds tree depth")
            metadata = {**item["metadata_json"], "topic": str(path.parent.relative_to(tree))}
            self.store.write(builder, uow, kind=item["kind"], before=item, path=str(path), metadata=metadata, expected_revision=item["revision"])
        replace_targets(builder, uow, {"type": "topic", "id": row["id"]}, "part_of", "topic", [parent["id"]] if parent else [])

    def _merge_people(self, builder, uow, args, options):
        keep = self.store.get(builder.ctx, args["document_id"], uow, kinds={"context_person"})
        drop = self.store.get(builder.ctx, args["merge_id"], uow, kinds={"context_person"})
        if keep["id"] == drop["id"]: raise DomainError("validation_error", "Cannot merge a person with itself")
        require_revision(keep, options.expected_revision)
        require_revision(drop, args["merge_revision"])
        keep_body, drop_body = split_document(self.store.read_bytes(keep, uow))[1], split_document(self.store.read_bytes(drop, uow))[1]
        aliases = list(dict.fromkeys([*keep["metadata_json"].get("aka", []), drop["metadata_json"].get("name", ""), *drop["metadata_json"].get("aka", [])]))
        metadata = {**keep["metadata_json"], "aka": [alias for alias in aliases if alias and alias != keep["metadata_json"].get("name")],
                    "merged_from": [*keep["metadata_json"].get("merged_from", []), {"id": drop["id"], "metadata": drop["metadata_json"]}]}
        body = keep_body.rstrip() + "\n\n### Merged From " + drop["metadata_json"].get("name", drop["id"]) + "\n\n" + drop_body
        self.store.write(builder, uow, kind="context_person", before=keep, body=body, metadata=metadata, expected_revision=keep["revision"])
        self.store.archive(builder, uow, drop, expected_revision=drop["revision"])
        for edge in rows(uow, "relationships", builder.ctx.business_id,
                where="(from_type='person' AND from_id=?) OR (to_type='person' AND to_id=?)", parameters=(drop["id"], drop["id"])):
            values = {prefix + "_id": keep["id"] for prefix in ("from", "to") if edge[prefix + "_type"] == "person" and edge[prefix + "_id"] == drop["id"]}
            builder.record("relationships", edge["id"], edge, {**values, "updated_at": builder.at})

    # -- validation and application --------------------------------------------------------------------------------------

    def validate_documents(self, ctx, plan, uow):
        """Reject old prepared/signoff plans and indirect document writes into the journal or another project's notes."""
        for change in plan.changes:
            if change.resource.resource_type != "documents":
                continue
            current = uow.repository("documents", business_id=change.resource.business_id).get(change.resource.resource_id)
            if change.values.get("kind") in JOURNAL_KINDS or current and current["kind"] in JOURNAL_KINDS:
                require_owner(ctx, uow)
            business_notes.require_project_access(ctx, current, change.values.get("kind"), change.resource.business_id)

    def validate_targets(self, ctx, plan, uow, *, global_note_operations=GLOBAL_NOTE_OPS):
        self.check_global_notes(plan.operation_name, ctx, plan.arguments, uow, global_note_operations)
        self.validate_documents(ctx, plan, uow)
        if plan.operation_name == "context_people_merge":
            expected = {target.resource.resource_id for target in plan.expected_targets if target.resource.resource_type == "relationships"}
            identity = plan.arguments["merge_id"]
            for edge in rows(uow, "relationships", ctx.business_id,
                    where="(from_type='person' AND from_id=?) OR (to_type='person' AND to_id=?)", parameters=(identity, identity)):
                if edge["id"] not in expected:
                    raise DomainError("revision_conflict", "A new person reference appeared after preparation")

    def refresh(self, plan, uow):
        """Opportunistic cache refresh of the plan's documents after their verified effects."""
        self.library.refresh_managed({change.resource.resource_id for change in plan.changes
            if change.resource.resource_type == "documents" and change.action != "policy_effect"}, uow)

    def result(self, ctx, plan, uow):
        """The documents this plan changed, their primary one and the deleted identities."""
        changed = [change for change in plan.changes if change.resource.resource_type == "documents"]
        documents = []
        for change in changed:
            if change.action not in {"delete", "policy_effect"}:
                row = uow.repository("documents", business_id=change.resource.business_id).get(change.resource.resource_id, required=True)
                # File receipts keep rows unavailable until this exact transaction finishes.
                document = {**{key: value for key, value in row.items() if key not in {"metadata_json", "source_refs_json", "owning_agent_id"}},
                    **classification(row), "metadata": row["metadata_json"], "source_refs": row["source_refs_json"], "agent_id": row["owning_agent_id"], "availability": "ready"}
                if row["kind"] == "agent_note":
                    related_business = self.store.related_business(row, uow)
                    if related_business is not None:
                        document["related_business"] = related_business
                documents.append(document)
        visible = [document for document in documents if document["kind"] != "context_index"]
        requested_id = plan.arguments.get("document_id", plan.arguments.get("upload_id"))
        primary = next((document for document in visible if document["id"] == requested_id), visible[0] if visible else None)
        return {"document": primary, "documents": documents,
                "deleted": [change.resource.resource_id for change in changed if change.action == "delete"]}

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        if plan.operation_name in {"context_people_update", "context_people_affiliation_set"} and any(c.resource.resource_type == "business_memberships" for c in plan.changes):
            row = uow.repository("principals").get(plan.arguments["document_id"], required=True)
            return {"person": person_details(row, uow, business_id=ctx.business_id)}
        self.refresh(plan, uow)
        return self.result(ctx, plan, uow)
