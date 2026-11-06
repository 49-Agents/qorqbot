"""Instruction assembly policy; the host supplies authorized sources and data.

Standing instructions precede the current identity, assignment and contextual data.
"""

SECTION_ORDER = (
    "Core Working Rules", "Universal Foundation", "User Preferences",
    "Scope Operating Brief", "Role Brief", "Role Work Loop", "Agentic Toolkit",
    "Agent inbox", "Work Orientation", "Engineering workspace and authority", "Identity And Assignment",
    "Ecosystem Orientation", "Projects", "Selected Project", "Context Home",
)
INSTRUCTION_SECTIONS = frozenset(SECTION_ORDER[:10])
LEADING_ORIENTATION = ("software-ownership", "startup")
COMMON_ORIENTATION = ("completion-report", "thread-replies", "owner-decisions", "trusted-handoffs")
CAPABILITIES = (
    (('viewbox_place',), 'viewbox-guidance'),
    (('environment_set',), 'environment-set'),
    (('environment_get',), 'environment-get'),
    (('operations_batch',), 'operation-batches'),
    (('tasks_overview',), 'task-overview'),
    (('environment_list',), 'environment-list'),
    (('files_link',), 'files-link-guidance'),
    (('notes_create', 'notes_get', 'plans_get', 'plans_set'), 'legacy-working-memory'),
    (('agents_list', 'tasks_list'), 'team-queue'),
    (('tasks_list', 'tasks_update'), 'backlog'),
    (('owner_conversation_get', 'owner_conversation_send'), 'private-owner-replies'),
    (('tasks_assign',), 'task-assignment'),
    (('tasks_create', 'tasks_update'), 'task-relations'),
    (('signoffs_get', 'signoffs_list'), 'signoff-evidence'),
    (('learnings_review_get', 'learnings_review_list'), 'learning-review'),
    (('context_expand', 'context_find', 'context_home', 'context_object'), 'context-reads'),
)


def orientation_tail(available_operations):
    paths = list(COMMON_ORIENTATION)
    if "business_briefing" in available_operations:
        paths.append("project-briefing")
    return (*paths, "working-memory", "capability-handoffs")


def capability_guidance(available_operations):
    return tuple(path for required, path in CAPABILITIES if set(required) <= available_operations)


def assemble_sections(header, sections):
    by_title = dict(sections)
    if len(by_title) != len(sections) or set(by_title) - set(SECTION_ORDER):
        raise ValueError("Unknown or duplicate mandatory instruction section")
    instructions = "\n\n".join(by_title[title] for title in SECTION_ORDER if title in by_title and title in INSTRUCTION_SECTIONS)
    context = header + "\n\n".join(by_title[title] for title in SECTION_ORDER if title in by_title and title not in INSTRUCTION_SECTIONS)
    return "\n\n".join(filter(None, (instructions, context)))


def split_sections(header, sections):
    return (assemble_sections('', [(title, body) for title, body in sections if title in INSTRUCTION_SECTIONS]),
            assemble_sections(header, [(title, body) for title, body in sections if title not in INSTRUCTION_SECTIONS]))
