"""Exact local project creation: one registered row, its memberships and an empty project folder, and nothing else.

Creation is a global aggregate: the new project does not exist at preparation time. Its frozen change holds the selected
identity, slug, metadata, owner and system memberships and the directory layout (``hq/`` and ``code/``, both empty). The folder is
made by the Operations ``create_directory_tree`` file effect (a receipt-bound private staging area published with no-replace
semantics, and recognized afterwards only by its recorded inode identity), then the row commits in the normal final
transaction. Creation writes no document, Task, routine, checklist, agent, Context input or external registration, starts no
session and calls no provider; independent explicit services provision on their own explicit requests.
"""
import os
import re
import unicodedata

from qorqut_operations.application.plans import ref
from qorqut_operations.contracts import (DomainError, ExpectedTarget, FieldChange, PreparedChange,
    PreviewResource, ResourceRef, StructuredPreview, new_id, plain)
from qorqut_operations.storage.directories import RegisteredDirectoryTarget, observe_directory
from qorqut_operations.storage.directory_layout import ACTION as DIRECTORY_ACTION, checked_values, directory_values

ACTION = "create_business"
DIRECTORIES = "business_creation_directories"
NAMES = "business_creation_names"
LAYOUT = ("code", "hq")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}")
FALLBACK_SLUG = "project"
SUGGESTION_LIMIT = 1000
# What a creation freezes in its one record change, and the only keys it writes into the new project's configuration.
VALUE_KEYS = frozenset({"business", "owner_id", "worker_ids", "directories"})
CONFIG_KEYS = frozenset({"one_liner", "code"})
# The inert storage defaults of a new row: no lifecycle-stage form or workflow reads them.
STAGE, TIMEZONE = "idea", "UTC"


def derive_slug(name: str) -> str:
    """The deterministic folder name proposed for a project name.

    Normalized ASCII words separated by single hyphens, ``project-`` prefixed when the result does not start with a letter,
    trimmed to 64 characters, and ``project`` when nothing remains. The result always matches ``SLUG``."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if not words:
        return FALLBACK_SLUG
    if not words[0].isalpha():
        words = FALLBACK_SLUG + "-" + words
    return words[:64].rstrip("-")


def numbered(slug: str, number: int) -> str:
    suffix = f"-{number}"
    return slug[:64 - len(suffix)].rstrip("-") + suffix


class Creation:
    """The creation rules over the facts the operations object was constructed with (see ``BusinessCreationOperations``).

    ``operations.project_root(row)``, ``project_hq(row)`` and ``relative_path(path)`` are the host's own validating path
    functions; they are called at every point the rules need a location and their results are never kept."""

    def __init__(self, operations):
        self.operations = operations

    def register_targets(self):
        app = self.operations.application
        app.register_target_resolver(NAMES, self.name_snapshot)
        app.register_target_resolver(DIRECTORIES, self.directory_snapshot)

    # --- the registered slug and folder ------------------------------------------------------------------------------

    @staticmethod
    def name_snapshot(ctx, resource, uow):
        return uow.connection.execute("SELECT id,revision FROM businesses WHERE lower(slug)=?", (resource.resource_id.lower(),)).fetchone()

    def directory_target(self, resource) -> RegisteredDirectoryTarget:
        """The one registered creation target: the project root below the canonical projects directory, staged privately."""
        slug = resource.resource_id
        if (not isinstance(slug, str) or SLUG.fullmatch(slug) is None or resource.resource_type != DIRECTORIES
                or resource.business_id is not None or resource.namespace != "global"):
            raise DomainError("invalid_plan", "Invalid project folder")
        return RegisteredDirectoryTarget(self.operations.operations_root,
                                         self.operations.relative_path(self.operations.project_root({"slug": slug})),
                                         self.operations.staging_root)

    def directory_snapshot(self, ctx, resource, uow):
        digest = observe_directory(self.directory_target(resource))
        return {"content_hash": digest} if digest else None

    def variants(self, slug) -> list[str]:
        """The entries of the projects directory that spell this slug with other letter case.

        The slug is lower case, so an entry that equals it only ignoring case is never this creation's own folder (which is
        published under the exact spelling). It is another writer's, whatever the filesystem's case rules, and it is neither
        adopted, renamed nor replaced; it only reserves the name."""
        # The canonical resolver refuses a symlinked projects directory or destination before anything is listed.
        folder = self.operations.project_root({"slug": slug})
        try:
            with os.scandir(folder.parent) as entries:
                return sorted(entry.name for entry in entries if entry.name != slug and entry.name.lower() == slug)
        except FileNotFoundError:
            return []

    def occupied(self, slug, uow) -> bool:
        """Whether a registered row, any folder or any case-variant entry already holds this slug."""
        if self.name_snapshot(None, ResourceRef(NAMES, slug, None, "global"), uow) is not None:
            return True
        return os.path.lexists(self.operations.project_root({"slug": slug})) or bool(self.variants(slug))

    def taken(self, slug, uow) -> DomainError:
        """The refusal for a taken slug: the first available numbered suggestion, never an adoption or a choice."""
        suggested = next((candidate for candidate in (numbered(slug, number) for number in range(2, SUGGESTION_LIMIT))
                          if not self.occupied(candidate, uow)), None)
        details = {"slug": slug, **({"suggested_slug": suggested} if suggested else {})}
        return DomainError("revision_conflict", "That project folder name is taken; choose another one", details=details)

    def require_free(self, slug, uow):
        if self.occupied(slug, uow):
            raise self.taken(slug, uow)

    def require_no_variant(self, slug, uow):
        """Keep the case-insensitive reservation after preparation: a sibling that appeared since refuses every later phase.

        Only the other spellings are examined. The exact destination is the business of the file effect and the plan's
        targets, which recognize this creation's own published directory by its receipt and refuse any other."""
        if self.variants(slug):
            raise self.taken(slug, uow)

    # --- bounded system memberships ---------------------------------------------------------------------------------

    def workers(self, uow):
        """The already registered bounded system programs that require project scope, revalidated; no principal is created."""
        authority = self.operations.worker_authority
        if authority is None:
            raise DomainError("configuration_required", "Configure background programs before creating a project")
        identities = []
        for program in authority.programs:
            if "business" in program.scope_kinds:
                identity = authority.worker_id(program.id, uow)
                authority.require_worker(identity, None, uow)
                identities.append(identity)
        return identities

    # --- prepare, validate, apply -------------------------------------------------------------------------------------

    def prepare(self, builder, args, uow):
        name = args["name"].strip()
        summary = args.get("summary", "").strip()
        if not name or len(name) > 200:
            raise DomainError("validation_error", "A project needs a name of up to 200 characters",
                              field_errors={"name": "Enter a name of up to 200 characters"})
        if len(summary) > 2000:
            raise DomainError("validation_error", "The description is limited to 2000 characters",
                              field_errors={"summary": "Use up to 2000 characters"})
        installation = uow.repository("installation").get("service", required=True)
        owner = uow.repository("principals").get(installation["owner_principal_id"], required=True)
        if owner["kind"] != "human" or not owner["active"]:
            raise DomainError("forbidden", "Project creation requires an active installation owner")
        builder.expect(ref("installation", "service", None, "global"), installation)
        builder.expect(ref("principals", owner["id"], None, "global"), owner)
        worker_ids = self.workers(uow)
        for identity in worker_ids:
            builder.expect(ref("principals", identity, None, "global"), uow.repository("principals").get(identity, required=True))
        slug = args.get("slug") or derive_slug(name)
        self.require_free(slug, uow)
        name_ref = ResourceRef(NAMES, slug, None, "global")
        builder.targets[name_ref] = ExpectedTarget(name_ref, exists=False)
        folder_ref = ResourceRef(DIRECTORIES, slug, None, "global")
        builder.targets[folder_ref] = ExpectedTarget(folder_ref, exists=False)
        project_id = new_id()
        folder, hq = self.operations.project_root({"slug": slug}), self.operations.project_hq({"slug": slug})
        business = {"id": project_id, "slug": slug, "workspace": str(folder), "hq_path": str(hq), "name": name,
                    "stage": STAGE, "timezone": TIMEZONE, "config_json": {"one_liner": summary, "code": []}}
        builder.record("businesses", project_id, None, {"business": business, "owner_id": owner["id"], "worker_ids": worker_ids,
                                                         "directories": list(LAYOUT)},
                       action=ACTION, business_id=None, namespace="global", label=name)
        builder.changes.append(PreparedChange("file", folder_ref, DIRECTORY_ACTION, directory_values(LAYOUT)))
        projects = self.operations.projects_directory
        shown = PreviewResource(DIRECTORIES, slug, f"{projects}/{slug}")
        builder.resources.append(shown)
        builder.previews.append(StructuredPreview(shown, (FieldChange("folders", None, [f"{projects}/{slug}/{path}" for path in LAYOUT]),)))
        builder.effects.append("Creates only this new project, the owner's membership, the memberships of the already registered "
                               f"project-scoped system programs, and the empty folder {projects}/{slug} with hq/ and code/. "
                               "It creates no agents, documents, Tasks, routines, Context inputs, external accounts, domains, "
                               "repositories or sessions. Every active global agent reaches the new project at once.")
        return builder.build()

    def check_frozen(self, plan, uow):
        """The exact record and directory changes of a creation, or ``DomainError``: nothing else may ride along."""
        created = [change for change in plan.changes if change.action == ACTION]
        folders = [change for change in plan.changes if change.kind == "file"]
        if (len(created) != 1 or len(folders) != 1 or len(plan.changes) != 2 or folders[0].action != DIRECTORY_ACTION
                or folders[0].resource.resource_type != DIRECTORIES):
            raise DomainError("invalid_plan", "The plan does not create exactly one project")
        values = plain(created[0].values)
        business = values.get("business") if set(values) == VALUE_KEYS else None
        if (not isinstance(business, dict) or SLUG.fullmatch(str(business.get("slug"))) is None
                or set(business.get("config_json") or ()) != CONFIG_KEYS or values["directories"] != list(LAYOUT)
                or business["stage"] != STAGE or business["timezone"] != TIMEZONE):
            raise DomainError("invalid_plan", "The project differs from what preparation writes")
        slug = business["slug"]
        reference = ref("businesses", business["id"], None, "global")
        if (created[0].resource != reference or folders[0].resource != ResourceRef(DIRECTORIES, slug, None, "global")
                or tuple(checked_values(folders[0].values)) != LAYOUT):
            raise DomainError("invalid_plan", "The project folder differs from the project")
        if (business["workspace"] != str(self.operations.project_root({"slug": slug}))
                or business["hq_path"] != str(self.operations.project_hq({"slug": slug}))):
            raise DomainError("revision_conflict", "The project location changed after preparation")
        owner = uow.repository("principals").get(values["owner_id"])
        installation = uow.repository("installation").get("service", required=True)
        if owner is None or installation["owner_principal_id"] != owner["id"] or owner["kind"] != "human" or not owner["active"]:
            raise DomainError("revision_conflict", "The installation owner changed after preparation")
        self.require_no_variant(slug, uow)
        return values

    def validate(self, ctx, plan, uow):
        values = self.check_frozen(plan, uow)
        if set(values["worker_ids"]) != set(self.workers(uow)):
            raise DomainError("revision_conflict", "Background programs changed after preparation")

    def apply(self, ctx, plan, uow):
        values = self.check_frozen(plan, uow)
        if set(values["worker_ids"]) != set(self.workers(uow)):
            raise DomainError("revision_conflict", "Background programs changed after preparation")
        business = values["business"]
        uow.repository("businesses").insert(business)
        memberships = uow.repository("business_memberships", business_id=business["id"])
        memberships.insert({"principal_id": values["owner_id"], "is_owner": True})
        for identity in values["worker_ids"]:
            memberships.insert({"principal_id": identity})
        return {"business": {"id": business["id"], "slug": business["slug"], "name": business["name"],
                             "summary": business["config_json"]["one_liner"], "workspace_path": business["workspace"],
                             "hq_path": business["hq_path"]},
                "business_id": business["id"], "slug": business["slug"]}
