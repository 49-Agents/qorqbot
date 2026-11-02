"""Deterministic, bounded mandatory instruction composition."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import Mapping

from markdown_it import MarkdownIt

from bazarlyk.catalog.packages import ArtifactPackage, content_hash
from qorqut_operations.contracts import DomainError, canonical_bytes
from .models import AgentProfile
from .roles import SUPPORTED_GLOBAL_ROLES
from . import role_workflow
from bazarlyk.agentic.text import bundled, render

# A generous total delivery budget, roughly 20k tokens for ordinary English.
# This is a character bound, not an assertion about a model's tokenizer.
MAX_CONTEXT_CHARACTERS = 120000
VIEWBOX_GUIDANCE = (bundled("host", "app/bazarlyk/agentic/assets/viewbox-guidance.md"))


FILES_LINK_GUIDANCE = (bundled("host", "app/bazarlyk/agentic/assets/files-link-guidance.md"))


@dataclass(frozen=True)
class ContextSource:
    document_id: str
    text: str
    revision: int | str
    content_hash: str
    source_path: str
    source_kind: str = "document"
    section_selector: str | None = None
    preserve_text: bool = False

    def selected_text(self) -> str:
        if self.section_selector:
            return markdown_section(self.text, self.section_selector)
        return self.text if self.preserve_text else self.text.strip()


def markdown_section(text: str, title: str = "Agent Brief") -> str:
    """Select one real Markdown heading, including nested headings and fences."""
    tokens = MarkdownIt("commonmark").parse(text)
    matches = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and tokens[index + 1].content.strip() == title:
            matches.append(index)
    if len(matches) != 1:
        raise DomainError("missing_context_section" if not matches else "ambiguous_context_section",
                          f"Expected exactly one Markdown heading '{title}'")
    index = matches[0]
    token = tokens[index]
    start = token.map[1]
    level = int(token.tag[1:])
    lines = text.splitlines()
    end = len(lines)
    for following in tokens[index + 3:]:
        if following.type == "heading_open" and int(following.tag[1:]) <= level:
            end = following.map[0]
            break
    result = "\n".join(lines[start:end]).strip()
    if not result:
        raise DomainError("missing_context_section", f"Mandatory Markdown section '{title}' is empty")
    return result


def business_aliases(row) -> list[str]:
    """Explicit settings aliases, then slug, domain, website and email domain; never the display name."""
    config = row.get("config_json") or {}
    if isinstance(config, str):
        config = json.loads(config)
    explicit = config.get("aliases") if isinstance(config.get("aliases"), (list, tuple)) else ()
    website = config.get("website") if isinstance(config.get("website"), str) else ""
    email = config.get("email") if isinstance(config.get("email"), str) else ""
    candidates = [*explicit, config.get("name"), row.get("slug"), config.get("domain"),
                  urlsplit(website).hostname if "://" in website else website, email.rpartition("@")[2]]
    key = lambda text: re.sub(r"[^a-z0-9]", "", text.lower())
    seen, result = {key(str(row.get("display_name") or row.get("name") or ""))}, []
    for item in candidates:
        text = " ".join(item.split())[:60] if isinstance(item, str) else ""
        if text and key(text) and key(text) not in seen:
            seen.add(key(text))
            result.append(text)
    return result


def business_directory(businesses, *, agentic_view=None) -> str:
    """One entry per active project: name, ID, stage and one-liner."""
    lines = []
    def name(row):
        return row.get("display_name") or row.get("name") or row["id"]
    for item in sorted(businesses, key=lambda row: name(row).lower()):
        config = item.get("config_json") or {}
        if isinstance(config, str):
            config = json.loads(config)
        one_liner = " ".join(str(config.get("one_liner") or "").split())
        if len(one_liner) > 300:
            one_liner = one_liner[:299].rsplit(" ", 1)[0] + "…"
        aliases = business_aliases(item)
        line = (f"- {name(item)} ({item['id']}); stage: {item.get('stage') or 'unknown'}."
                + (f" Also called: {', '.join(aliases)}." if aliases else "") + (f" {one_liner}" if one_liner else ""))
        lines.append(line)
    if not lines:
        return "No active authorized projects."
    return ((bundled("host", "app/bazarlyk/agentic/assets/project-aliases.md") if agentic_view is None else
             render(agentic_view, "host", "app/bazarlyk/agentic/assets/project-aliases.md"))
            + "\n".join(lines))


def compose_context(profile: AgentProfile, role: ArtifactPackage, sources: Mapping[str, ContextSource], *,
                    effective_config_id: str, workspace: str, business: Mapping | None,
                    allowed_businesses: tuple[Mapping, ...] = (), owner_name: str = "Owner",
                    available_operations: frozenset[str] = frozenset(), context_home: str | None = None, agentic_view=None,
) -> dict:
    entries, rendered = [], []

    def add(title: str, source: ContextSource, reason: str) -> None:
        try:
            body = source.selected_text()
        except DomainError as exc:
            raise DomainError(exc.code, f"{source.document_id}: {exc.message}") from exc
        if not body.strip():
            raise DomainError("missing_context", f"Mandatory context is empty: {source.document_id}")
        header = f"## {title}\n[source={source.document_id}; revision={source.revision}; hash={source.content_hash}]"
        rendered.append(header + "\n" + body)
        entries.append({"document_id": source.document_id, "source_id": source.document_id,
                        "source_kind": source.source_kind, "source_path": source.source_path,
                        "section_selector": source.section_selector, "revision": source.revision,
                        "hash": source.content_hash, "required": True, "character_count": len(body),
                        "inclusion_reason": reason})

    def derived(title: str, body: str, key: str, reason: str) -> None:
        add(title, ContextSource(key, body, profile.revision, content_hash(body.encode()),
                                 "profile:" + profile.agent_id, "profile"), reason)

    def required(key: str) -> ContextSource:
        if key not in sources:
            raise DomainError("missing_context", f"Missing registered mandatory context: {key}")
        return sources[key]

    # Only a supported global is rendered; a business-home or retired-role identity is history, never shown as a global.
    if profile.scope.kind != "ecosystem" or profile.scope.business_id is not None or role.id not in SUPPORTED_GLOBAL_ROLES:
        raise DomainError("invalid_profile", "Mandatory context renders only a supported global agent with ecosystem scope")
    identity = (render(agentic_view, "host", "app/bazarlyk/agentic/assets/identity.md", DISPLAY_NAME=str(profile.data['display_name']), NAME=str(profile.data['name']), AGENT_ID=str(profile.agent_id), ROLE_ID=str(role.id), ROLE_VERSION=str(role.version), OWNER_NAME=str(owner_name), ASSIGNMENT=str(profile.data['assignment_brief']), PROFILE_REVISION=str(profile.revision), CONFIGURATION=str(effective_config_id)))
    identity += render(agentic_view, "host", "app/bazarlyk/agentic/assets/scope.md", PROJECT_COUNT=str(len(allowed_businesses)))
    identity += "\nGlobal capabilities: " + (", ".join(profile.scope.global_capabilities) or "none") + "."
    derived("Identity And Assignment", identity, "profile:" + profile.agent_id, "Mandatory current identity and assignment")
    add("Universal Foundation", required("foundation"), "Common rules for every registered agent")
    add("User Preferences", required("user-preferences/preferences"), "Canonical preferences for every agent")
    add("Scope Operating Brief", required(role.manifest["scope_brief"]), "Selected scope has no implicit exceptions")
    brief = agentic_view.role_brief(role)
    add("Role Brief", ContextSource(f"role:{role.id}@{role.version}", brief["body"],
        brief["revision"], brief["sha256"], brief["path"], "agentic"), "Current authored mandatory responsibilities")
    role_instructions = render(agentic_view, "host", "app/bazarlyk/agentic/assets/role-workflow.md")
    add("Role Work Loop", ContextSource("core:role-workflow", role_instructions, 1,
        content_hash(role_instructions.encode()), "qorqut-core:role-workflow", "core"),
        "Standing startup, continuation and idle behavior")
    toolkit = render(agentic_view, "host", "app/bazarlyk/agentic/assets/toolkit.md",
                     TOOLKIT_COMMAND=shlex.quote(str(Path(workspace) / ".bazarlyk/agentic")))
    add("Agentic Toolkit", ContextSource("core:agentic-toolkit", toolkit, 1, content_hash(toolkit.encode()),
        "qorqut-core:agentic-toolkit", "core"), "Current native authoring, catalog and progress guidance")
    add("Ecosystem Orientation", required("ecosystem"), "Approved ecosystem facts")
    derived("Projects", business_directory(allowed_businesses, agentic_view=agentic_view),
            "businesses:" + profile.agent_id, "Active authorized projects with a short orientation")
    if business is not None:
        # The shared published graph (Context Home below) is the current Context orientation. The authored
        # ICP and Brand documents stay ordinary scoped sources read through their document APIs; they are neither an
        # always-injected brief nor a fallback for an unreadable graph.
        aliases = business_aliases(business)
        derived("Selected Project", render(agentic_view, "host", "app/bazarlyk/agentic/assets/selected-project.md", PROJECT_NAME=str(business['display_name']), PROJECT_ID=str(business['id']), STAGE=str(business['stage']), PROJECT_REVISION=str(business['revision']))
                + (f" Also called: {', '.join(aliases)}." if aliases else "") + render(agentic_view, "host", "app/bazarlyk/agentic/assets/selected-scope.md"),
                "business:" + business["id"], "Explicit permitted project orientation")
    if context_home:
        derived("Context Home", context_home, "context:published",
                render(agentic_view, "host", "app/bazarlyk/agentic/assets/context-provenance.md"))
    if business is not None:
        config = business.get("config_json", {})
        if isinstance(config, str):
            config = json.loads(config)
        authority = config.get("engineering", {})
        derived("Engineering workspace and authority",
            render(agentic_view, "host", "app/bazarlyk/agentic/assets/code-root.md", WORKSPACE=str(business['workspace']))
            + "Authority (owner/Ordubashy-managed; absent targets are unconfigured): " + canonical_bytes(authority).decode()
            + "\nRegistered repositories: " + canonical_bytes(config.get("code", [])).decode(),
            "engineering:" + business["id"], "Current repository and deployment ownership; no credential values")
    plugins = ", ".join(f"{item['id']}@{item['version']}" for item in profile.data["plugins"]) or "none"
    parameters = "\n".join(f"{item['id']} parameters: {canonical_bytes(item['config']).decode()}" for item in profile.data["plugins"])
    orientation = [render(agentic_view, "host", "app/bazarlyk/agentic/assets/workspace.md", WORKSPACE=str(workspace))]
    orientation.append(render(agentic_view, "host", "app/bazarlyk/agentic/assets/software-ownership.md"))
    orientation.append(render(agentic_view, "host", "app/bazarlyk/agentic/assets/startup.md"))
    from .preferences import plugin_discovery_guidance
    orientation.append(plugin_discovery_guidance())
    orientation.append(
        render(agentic_view, "host", "app/bazarlyk/agentic/assets/completion-report.md"))
    orientation.append(
        render(agentic_view, "host", "app/bazarlyk/agentic/assets/thread-replies.md"))
    if "telegram_send" in available_operations:
        orientation.append(
            render(agentic_view, "host", "app/bazarlyk/agentic/assets/telegram-replies.md"))
    orientation.append(
        render(agentic_view, "host", "app/bazarlyk/agentic/assets/owner-decisions.md"))
    orientation.append(render(agentic_view, "host", "app/bazarlyk/agentic/assets/trusted-handoffs.md"))
    if "business_briefing" in available_operations:
        orientation.append(render(agentic_view, "host", "app/bazarlyk/agentic/assets/project-briefing.md"))
    orientation.append(render(agentic_view, "host", "app/bazarlyk/agentic/assets/working-memory.md"))
    orientation.append(
        render(agentic_view, "host", "app/bazarlyk/agentic/assets/capability-handoffs.md"))
    if {'inbox_list', 'inbox_acknowledge'} <= available_operations:
        inbox_instructions = render(agentic_view, "host", "app/bazarlyk/agentic/assets/inbox.md")
        add("Agent inbox", ContextSource("core:agent-inbox", inbox_instructions, 1,
            content_hash(inbox_instructions.encode()), "qorqut-core:agent-inbox", "core"),
            "Built-in agent startup and inbox handling")
    for required_tools, guidance in (
        ({"viewbox_place"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/viewbox-guidance.md")),
        ({"environment_set"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/environment-set.md")),
        ({"environment_get"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/environment-get.md")),
        ({"operations_batch"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/operation-batches.md")),
        ({"tasks_overview"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/task-overview.md")),
        ({"environment_list"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/environment-list.md")),
        ({"files_link"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/files-link-guidance.md")),
        ({"plans_get", "plans_set", "notes_get", "notes_create"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/legacy-working-memory.md")),
        ({"agents_list", "tasks_list"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/team-queue.md")),
        ({"tasks_update", "tasks_list"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/backlog.md")),
        ({"owner_conversation_get", "owner_conversation_send"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/private-owner-replies.md")),
        ({"tasks_assign"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/task-assignment.md")),
        ({"tasks_create", "tasks_update"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/task-relations.md")),
        ({"signoffs_list", "signoffs_get"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/signoff-evidence.md")),
        ({"learnings_review_list", "learnings_review_get"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/learning-review.md")),
        ({"context_home", "context_find", "context_object", "context_expand"}, render(agentic_view, "host", "app/bazarlyk/agentic/assets/context-reads.md")),
    ):
        if required_tools <= available_operations:
            orientation.append(guidance)
    work = ("\n".join(orientation) + "\n" + render(agentic_view, "host", "app/bazarlyk/agentic/assets/plugins.md", PLUGINS=str(plugins))
            + (parameters + "\n" if parameters else "")
            + ((render(agentic_view, "host", "app/bazarlyk/agentic/assets/context-evidence.md")) if context_home else
               render(agentic_view, "host", "app/bazarlyk/agentic/assets/context-unavailable.md"))
            + render(agentic_view, "host", "app/bazarlyk/agentic/assets/current-configuration.md", CONFIGURATION=str(effective_config_id)))
    derived("Work Orientation", work, "work:" + profile.agent_id, "Queues, own documents, selected abilities, and expanded evidence roots")
    bundle = (render(agentic_view, "host", "app/bazarlyk/agentic/assets/configuration-header.md", CONFIGURATION=str(effective_config_id))
              + "\n\n".join(rendered))
    if len(bundle) > MAX_CONTEXT_CHARACTERS:
        raise DomainError("context_budget_exceeded", f"Mandatory context contains {len(bundle)} characters; maximum is {MAX_CONTEXT_CHARACTERS}",
                          details={"character_count": len(bundle), "limit": MAX_CONTEXT_CHARACTERS,
                                   "sources": [{"id": entry["source_id"], "characters": entry["character_count"]} for entry in entries]})
    return {"rendered": bundle, "entries": entries, "character_count": len(bundle), "bundle_hash": content_hash(bundle.encode())}
