# Context Relativity

## Model

Business context is a set of existing items connected by directional relationships.
Items retain their domain storage and tools. Context Material is the common item
category for documents, excerpts, posts, published content and conversations.
People, topics, tasks, stories, tables,
and agents remain distinct item types. Linked work also retains references to
requests, actions, and signoffs.

Materials have three independent classification fields: `material_type` describes
what the text contains; `origin` records how it arrived (`upload`, `email`,
or `authored`); `tags` are descriptive labels. None controls access,
assignment or scheduling. Domain resource permissions still apply. Existing
document `kind` and paths identify storage/access adapters, not classification.
Published Content is a Material with `material_type: content`, not a second copy.
Approvals and scheduling requests remain separate operational records.

`context_items_list` filters by `material_type`, `origin`, or `tag` across
documents and email conversations. `context_list`/`context_search` use the same
filters for the text library, including published content. Email bodies retain
their plugin-controlled conversation tool. `materials_update` changes type/tags
with revision checks and never rewrites file bytes. Origin is not editable;
email's conversation type, published content's type and raw uploads' mixed type
are fixed. Registry classification overrides legacy frontmatter classification.
Per-agent notes/plans, owner notes/checklists and Learnings retain their existing
stores, tools and behavior and are excluded from this classification editor.

There is no dedicated Research domain or toolset. Former research documents are
ordinary document Materials; historical source paths, contents, metadata and
relationship IDs are preserved. Use shared context discovery and reading tools.
The word research can still be an ordinary topic, task label or description of
work; it no longer selects a separate storage or workflow.

Each relationship has its own ID, business scope, typed source and target IDs,
role, free-form explanation, creation time, and last-edit time. Multiple edges,
including duplicates, are allowed. Predefined roles are preferred; custom roles
are allowed when none fits. Part-of relationships allow multiple parents. Edges
can be edited or removed without a relationship-history feature. Relationships
do not confer access or wake agents.

One relationship store replaces Story links, task assignments and dependencies,
context topic membership, topic hierarchy, and person mentions. Existing domain
tools are input adapters, not competing stores. Task assignment accepts multiple
assignees sharing one task/status and drives all task queues from these edges.
Assignment writes, edits, and removals require task-tool policy, not unrestricted
edge CRUD. Existing email assignment rules likewise remain domain-controlled.

Archive is the normal item lifecycle, preserving content and edges. Searches,
queues, and maps exclude archived items by default and can explicitly include
them. Archiving does not cascade to children. Edge removal remains deletion.

## Filing And Source Fidelity

Direct uploads are preserved source records and never appear as map nodes.
Filing is triggered by owner uploads, not by incoming email. Email synchronization
directly maintains conversation material, without an AI filing pass.

The filer selects source spans and code copies those spans verbatim. It does not
regenerate source text. Extracted conversations preserve all supplied messages,
speaker labels, timestamps, ordering, whitespace, and Unicode. Topic excerpts can
omit irrelevant passages, but selected passages remain exact. Original uploads
and source references remain available outside the map.

The filer may create context materials, topics, and people, append exact source
material to clearly matching existing items, and maintain its own descriptions
and summaries. Ambiguous identity matches remain separate. It cannot overwrite
human-written descriptions or notes, appoint managers, change assignments, edit
task instructions or agent profiles, or create Stories. It can relate material to
existing Stories and maintain their Background. Story Details belongs to humans
and business agents; Background belongs to filing. No standalone Fact type.

Filing has a narrow input schema and may create only descriptive/provenance edges
such as concerns, mentions, extracted_from, and part_of. It is not granted general
relationship tools. Conversation matching uses explicit evidence, not just a
shared subject. Contradictory source claims remain attributed rather than being
silently reconciled as truth.

## Presentation

The Context Map supports pan, zoom, node positioning, search, selection, and
navigation. Connections come from stored edges, not paths or metadata. It shows
direction, distinct parallel edges, relationship roles and context, and all
supported item types. Direct uploads remain accessible as sources but
are excluded from map nodes and their incident map edges. Document-outline mind
maps are content summaries, not an alternate relationship authority.

Generic relationship creation/editing is agent-only initially; the dashboard
reads relationships. Existing domain workflows retain their dedicated controls.

## Shared Context Review

Every context item uses the same acknowledgment mechanism. Each endpoint can
review the other item's evidence for its own work. The
decision is shared by all agents working on that item, not per-agent read state.
For example, reviewing a conversation for a Story does not acknowledge it for a
task that also depends on it. Direction remains the relationship's meaning, not
a restriction on which endpoint can inspect relevant context.

Reading, filing, linking, and automatic synchronization never acknowledge context.
An agent explicitly records incorporated or no_change with an explanation.
Review records retain actor, commit timestamp, reviewed endpoint, relationship,
and source fingerprint in immutable history. A changed source or changed edge
meaning becomes pending again. Explicit reopening creates another immutable
decision. Removed edges cease to appear in pending lists but preserve historical
decisions; a new edge starts pending. Review metadata itself does not change the
source fingerprint or recursively make other items pending.

Use shared review-state rows for concurrency, separate from edge timestamps and
immutable decisions. Acknowledgments require the observed source fingerprint,
edge revision, and review-state revision. Optional domain edits compose with the
acknowledgment in one transaction through existing domain validation and policy;
they cannot bypass assignment, Story close/reopen, source protection, or signoff.
Existing Story review IDs, decisions, actor attribution, and current
acknowledgments are preserved in the common mechanism. Story tools remain adapters onto
it, not a separate review store. Filer permissions exclude acknowledgment tools.

## Verification

Core coverage lives in `test_context_relationships.py`, `test_context_reviews.py`,
`test_filing_materials.py`, and the existing task, document, Story and email tests.
These exercise permissions, concurrency, upgrade preservation, archive/restore,
verbatim extraction and protected authored content. `test_filer_provider.py` is
an opt-in real-provider check using synthetic inputs, not business correspondence.

Dashboard graph tests exercise topology, layout and navigation. The isolated
`test_relationships_browser.js` workflow covers shared decisions, lost-response
retries, history, reopening, Story Details and desktop/mobile rendering.
Agent MCP exposure is verified separately from Claude Code's behavior; actual
Claude sessions remain an owner acceptance check.
