"""Ordubashy's two tools for downloadable extensions: ``extensions_install`` (an install, or the update of an installed
one to the newer version in the index) and ``extensions_remove``.

Both are held for the owner's approval: the policy (``composition/policy.py``) gives them to Ordubashy alone, always as a
Signoff, and denies them to every other role. Preparing reads the index and the installed extensions and binds what is
asked for: for an install or update the id, version, address, checksum and size of the bundle from the index. The card is
short: name, version, the one-line summary and that Qorqbot restarts once.

Approving queues one job of the program ``extensions.change``. The job checks that it comes from exactly that approved
Signoff, then stages the bound change (``bootstrap/extension_change.py``) and asks Qorqbot to restart. A failure before
the restart (download, checksum, bundle check) changes nothing and is written on the Signoff as its plain sentence. After
the restart the service writes the result on the Signoff (``report``). A note on a Signoff is an inbox event to the agent
that proposed it, so the result reaches the one who asked, and Ordubashy tells the owner in chat.

The removal card has two buttons. "Remove" approves the proposal as it is, and the extension's private data stays.
"Remove and delete its data" is the owner's edit of the proposal's one editable field, ``delete_data``, followed by the
approval of the edited proposal; the dashboard reads the label and the edit from the preview. What the extension's records
created in the store (projects, tables, records) belongs to the owner and always stays.

The owner can make the same changes himself on the extension's Connections card (``owner_change``). His click is the
approval, so no Signoff is made: the change is checked as the tools check it, staged at once and applied at the restart,
and its result is shown on the card after the restart (``results``), as it is for a change Ordubashy asked for.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, apply_records, prepare_job, schema
from qorqut_operations.background.programs import Program, ProgramResult
from qorqut_operations.contracts import (DomainError, FieldChange, InvocationContext, PreviewResource, Principal,
                                         StructuredPreview, plain)

from qorqut import extension_index
from qorqut.bootstrap import extension_change as change
from qorqut.extensions import EXTENSION_ID
from qorqut.management.identity import configured_ordubashy

INSTALL_TOOL, REMOVE_TOOL = "extensions_install", "extensions_remove"
PROGRAM = "extensions.change"
MARKER = "extensions_change"
INSTALL_DESCRIPTION = (
    "Ask the owner to install a downloadable extension by its id, or to update an installed one when the index has a "
    "newer version. The owner decides on a card. After approval Qorqbot downloads it and restarts once, and the result "
    "comes to your inbox.")
REMOVE_DESCRIPTION = (
    "Ask the owner to remove a downloaded extension by its id. On the card the owner also decides whether its private "
    "data is deleted. After approval Qorqbot restarts once, and the result comes to your inbox. Projects, tables and "
    "records it created always stay.")
ARGUMENTS = schema({"extension": {"type": "string", "pattern": EXTENSION_ID.pattern}}, ("extension",))
# The job holds everything that was bound when the owner was asked; a removal has no bundle.
JOB = schema({"scope": {"const": "global"}, "action": {"enum": list(change.ACTIONS)},
              "extension": {"type": "string", "pattern": EXTENSION_ID.pattern},
              "version": {"type": ["string", "null"]}, "url": {"type": ["string", "null"]},
              "sha256": {"type": ["string", "null"]}, "size": {"type": ["integer", "null"], "minimum": 1},
              "platform": {"type": ["string", "null"]}, "delete_data": {"type": "boolean"}},
             ("scope", "action", "extension", "version", "url", "sha256", "size", "platform", "delete_data"))
RESTARTING = "Qorqbot restarts to apply this change."
CHANGED = "The index has changed since this was asked. Ask again."
ONLY_ORDUBASHY = "Only Ordubashy can ask the owner to install or remove an extension."
RESTARTS = "Qorqbot restarts once."
REPLACED = "Changes made to the extension's texts on this installation are replaced."
DATA_STAYS, DATA_DELETED = "Its private data stays.", "Its private data is deleted."
RECORDS_STAY = "Projects, tables and records it created stay."
REMOVE_LABEL, DELETE_LABEL = "Remove", "Remove and delete its data"
DELETE_NOTE = "Chose to delete the extension's private data as well."
DONE_WORDS = {change.INSTALL: "installed", change.UPDATE: "updated", change.REMOVE: "removed"}
_APPLIED_SIGNOFF = ("SELECT id FROM signoffs WHERE operation_name=? AND state='applied' "
                    "AND json_extract(apply_result_json,'$.data.job_id')=?")


class ExtensionTools:
    """The two tools, their program and the reporting of results, over the running instance. ``index``, ``app_version``,
    ``platform`` and ``machine`` default to this app's own; ``restart`` to the service's restart request."""

    def __init__(self, runtime, extensions, *, index=None, app_version=None, platform=None, machine=None, restart=None):
        from qorqut import extension_registry
        self.app, self.paths, self.programs = runtime.application, runtime.paths, runtime.programs
        self.signoffs, self.extensions = runtime.signoffs, extensions
        self.index = index or (lambda: extension_index.app_index(self.paths))
        self.app_version = app_version if app_version is not None else extension_registry.app_version()
        self.platform = platform or extension_registry.current_platform()
        self.machine, self.restart = machine, restart
        # The last change staged or applied for each extension in this process, in one sentence, for the owner's card.
        self.results = {}

    # -- registration --------------------------------------------------------------------------------------------------

    def register(self):
        common = dict(effect_kind="compound", supports_signoff=True, signoff_category="internal_change",
                      scope_kinds=frozenset({"global"}), exposure=frozenset({"mcp"}),
                      resolve_namespace=lambda ctx, args: "global", global_capability="read_global_context",
                      apply=self.apply, validate_targets=self.validate_targets)
        self.app.registry.register(OperationDescriptor(INSTALL_TOOL, ARGUMENTS, description=INSTALL_DESCRIPTION,
                                                       prepare=self.prepare_install, **common))
        self.app.registry.register(OperationDescriptor(REMOVE_TOOL, ARGUMENTS, description=REMOVE_DESCRIPTION,
                                                       prepare=self.prepare_remove, editable_fields=("delete_data",),
                                                       revise=self.revise_remove, **common))
        program = ChangeProgram(self)
        self.programs.register(Program(PROGRAM, 1, JOB, program.run, scope_kinds=("global",), external=True,
                                       recover=program.run, routine_allowed=False))
        return self

    def register_worker(self, uow):
        """Create the program's worker when it is missing, as ``BackgroundAuthority.register_workers`` does for every
        program, but for this one only; a disabled worker is never made active again."""
        identity = self.programs.authority.worker_id(PROGRAM, uow)
        actor = uow.repository("principals").get(identity)
        if actor is None:
            uow.repository("principals").insert({"id": identity, "kind": "system", "display_name": PROGRAM + " worker"})
        elif actor["kind"] != "system":
            raise DomainError("invalid_principal", "Program principal identity is occupied")

    # -- who may ask ---------------------------------------------------------------------------------------------------

    def caller(self, ctx, uow):
        """Exactly the configured Ordubashy in person, in Global."""
        ctx = self.app.current_context(ctx, uow)
        if (ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None or ctx.project_id is not None
                or configured_ordubashy(uow) != ctx.principal_id):
            raise DomainError("forbidden", ONLY_ORDUBASHY)
        return ctx

    @staticmethod
    def no_change_waiting(paths):
        if change.pending(paths) is not None:
            raise DomainError("invalid_transition", change.WAITING)

    def installed(self, connection):
        return {item.id: item for item in self.extensions.list(connection=connection) if item.folder is not None}

    # -- install and update ----------------------------------------------------------------------------------------------

    def install_target(self, ext_id, connection, index=None):
        """What installing ``ext_id`` from the index means here: ``(action, entry, bundle)``, or the refusal."""
        from qorqut.extension_registry import DOWNLOADABLE, admits
        index = index if index is not None else self.index()
        entry = index.entry(ext_id) if index is not None else None
        if entry is None:
            raise DomainError("not_found", f"No extension {ext_id!r} is in the index of downloadable extensions.")
        installed = self.installed(connection)
        # Whether the index version fits this computer and this app, judged as if it were not installed yet.
        others = [found for found in self.extensions.found if found.id != ext_id]
        fit = next(item for item in extension_index.offered(index, others, app_version=self.app_version,
                                                            platform=self.platform, machine=self.machine)
                   if item.id == ext_id)
        if fit.status == extension_index.UNSUPPORTED:
            raise DomainError("validation_error", fit.note)
        current = installed.get(ext_id)
        if current is None:
            action = change.INSTALL
        elif current.delivery != DOWNLOADABLE:
            raise DomainError("validation_error", f"{current.name or ext_id} comes with Qorqbot, so it changes only with "
                                                  "Qorqbot's own updates.")
        elif current.version is not None and admits(entry.version, ">" + current.version):
            action = change.UPDATE
        else:
            installed_as = " ".join(filter(None, (current.name or ext_id, current.version)))
            raise DomainError("validation_error", f"{installed_as} is installed, and the index has no newer version.")
        missing = [other for other in entry.requires_extensions if other not in installed]
        if missing:
            raise DomainError("validation_error", f"It needs the extension {missing[0]!r}, which is not installed. "
                                                  "Install that one first.")
        _, bundle = index.entry_for(ext_id, extension_index.bundle_platform(self.platform, self.machine))
        return action, entry, bundle

    def prepare_install(self, ctx, args, options, uow):
        self.caller(ctx, uow)
        self.no_change_waiting(self.paths)
        ext_id = args["extension"]
        action, entry, bundle = self.install_target(ext_id, uow.connection)
        job = {"scope": "global", "action": action, "extension": ext_id, "version": entry.version, "url": bundle.url,
               "sha256": bundle.sha256, "size": bundle.size, "platform": bundle.platform, "delete_data": False}
        after = RESTARTS if action == change.INSTALL else f"{RESTARTS} {REPLACED}"
        title = (f"Install the extension {entry.name} {entry.version}" if action == change.INSTALL
                 else f"Update the extension {entry.name} to {entry.version}")
        fields = (("extension", entry.name), ("version", entry.version), ("summary", entry.summary),
                  ("after_approval", after))
        return self._plan(ctx, INSTALL_TOOL, args, title, fields, job, uow)

    # -- removal -----------------------------------------------------------------------------------------------------------

    def removal_target(self, ext_id, connection):
        """The installed downloaded extension ``ext_id`` that may be removed now, or the refusal."""
        from qorqut.extension_registry import DOWNLOADABLE
        installed = self.installed(connection)
        current = installed.get(ext_id)
        if current is None:
            raise DomainError("not_found", f"No extension {ext_id!r} is installed.")
        if current.delivery != DOWNLOADABLE:
            raise DomainError("validation_error", f"{current.name or ext_id} comes with Qorqbot and cannot be removed.")
        needed_by = sorted(item.name or item.id for item in installed.values()
                           if item.manifest is not None and ext_id in item.manifest.requires_extensions)
        if needed_by:
            raise DomainError("validation_error", f"The extension {needed_by[0]} needs it. Remove that one first.")
        return current

    def prepare_remove(self, ctx, args, options, uow):
        self.caller(ctx, uow)
        self.no_change_waiting(self.paths)
        current = self.removal_target(args["extension"], uow.connection)
        return self._removal(ctx, args, current.name or current.id, current.version or "", False, uow)

    def revise_remove(self, ctx, original, changes, uow):
        """The owner's one choice on the removal card: delete the private data as well."""
        if changes != {"delete_data": True}:
            raise DomainError("validation_error", "Only deleting the extension's private data can be chosen here.")
        self.caller(ctx, uow)
        fields = self.fields(plain(original.preview))
        return self._removal(ctx, plain(original.arguments), fields.get("extension", ""), fields.get("version", ""),
                             True, uow)

    def _removal(self, ctx, args, name, version, delete_data, uow):
        job = {"scope": "global", "action": change.REMOVE, "extension": args["extension"], "version": None, "url": None,
               "sha256": None, "size": None, "platform": None, "delete_data": delete_data}
        after = f"{RESTARTS} {DATA_DELETED if delete_data else DATA_STAYS} {RECORDS_STAY}"
        fields = (("extension", name), ("version", version), ("after_approval", after))
        card = ({"approve_label": DELETE_LABEL, "amend_note": DELETE_NOTE} if delete_data else
                {"approve_label": REMOVE_LABEL, "amend_and_approve": {"label": DELETE_LABEL, "changes": {"delete_data": True}}})
        return self._plan(ctx, REMOVE_TOOL, args, f"Remove the extension {name}", fields, job, uow, card)

    # -- the plan both tools share -----------------------------------------------------------------------------------------

    def _plan(self, ctx, operation, args, title, fields, job, uow, card=None):
        builder = PlanBuilder(ctx, operation, plain(args), title, namespace="global", affected_scope={"kind": "global"})
        resource = PreviewResource("extension", job["extension"], "Extension")
        builder.resources.append(resource)
        builder.previews.append(StructuredPreview(resource, tuple(FieldChange(path, None, value) for path, value in fields)))
        shown = (len(builder.resources), len(builder.previews), len(builder.effects))
        prepare_job(builder, uow, PROGRAM, job, programs=self.programs,
                    definition_extra={MARKER: {"operation": operation, "agent_id": ctx.principal_id}})
        # The job stays in the plan's changes (it is what approval applies); the owner reads the card, not the queue row.
        del builder.resources[shown[0]:], builder.previews[shown[1]:], builder.effects[shown[2]:]
        plan = builder.build()
        # What the card offers besides Approve and Reject, read by the dashboard from the preview.
        return replace(plan, preview={**plain(plan.preview), **card}) if card else plan

    @staticmethod
    def fields(preview):
        return {field["path"]: field["after"] for item in preview.get("changes", []) for field in item.get("fields", [])}

    @staticmethod
    def _job_change(plan):
        found = [item for item in plan.changes if item.resource.resource_type == "jobs"]
        if len(found) != 1:
            raise DomainError("invalid_plan", "The prepared extension change is invalid.")
        return found[0]

    def validate_targets(self, ctx, plan, uow):
        self.caller(ctx, uow)
        values = plain(self._job_change(plan).values)
        job = values.get("arguments_json") or {}
        removal = plan.operation_name == REMOVE_TOOL
        if (values.get("kind") != PROGRAM or job.get("extension") != plan.arguments.get("extension")
                or (job.get("action") == change.REMOVE) != removal):
            raise DomainError("invalid_plan", "The prepared extension change is invalid.")
        # Checked again as ``prepare`` checked it, as things are now: a change made since (by the owner's own click, say)
        # refuses the approval with its plain sentence, and nothing is queued.
        self.no_change_waiting(self.paths)
        if removal:
            self.removal_target(job["extension"], uow.connection)
            return
        action, entry, bundle = self.install_target(job["extension"], uow.connection)
        if (action, entry.version, bundle.sha256) != (job["action"], job["version"], job["sha256"]):
            raise DomainError("invalid_transition", CHANGED)

    def apply(self, ctx, plan, uow):
        self.validate_targets(ctx, plan, uow)
        apply_records(ctx, plan, uow)
        return {"job_id": self._job_change(plan).resource.resource_id}

    # -- the owner's own click on the Connections card -------------------------------------------------------------------

    def offers(self, ext_id, connection, index=None):
        """What the owner's card offers for ``ext_id`` now: ``install`` or ``update`` with the index version, and
        ``remove``; each only when it would be accepted. Nothing while a change waits for the restart."""
        if change.pending(self.paths) is not None:
            return []
        found = []
        try:
            action, entry, _ = self.install_target(ext_id, connection, index)
            found.append({"action": action, "version": entry.version})
        except DomainError:
            pass
        try:
            self.removal_target(ext_id, connection)
            found.append({"action": change.REMOVE})
        except DomainError:
            pass
        return found

    def owner_change(self, ctx, ext_id, action, *, version=None, delete_data=False):
        """The owner's install, update or removal from the card, ``ctx`` being the owner: checked as the tools check it and
        staged at once, ``version`` being the one his card showed. Answers the sentence the card shows until the restart;
        a refusal or a failure before the restart is raised as its plain sentence and changes nothing."""
        with self.app.database.read() as uow:
            self.app.ensure_writes_open(uow)    # closed for writes as for the Signoff path through ``invoke``
            self.no_change_waiting(self.paths)
            if action == change.REMOVE:
                current = self.removal_target(ext_id, uow.connection)
                name = current.name or ext_id
            else:
                found, entry, bundle = self.install_target(ext_id, uow.connection)
                name = entry.name
                if (found, entry.version) != (action, version):
                    raise DomainError("invalid_transition", "The index has changed since the card was shown. Look again.")
        if action == change.REMOVE:
            outcome = change.stage_removal(self.paths, ext_id, requested_by=ctx.principal_id, delete_data=delete_data,
                                           restart=self.restart)
        else:
            outcome = change.stage(self.paths, SimpleNamespace(id=ext_id, version=version), bundle,
                                   requested_by=ctx.principal_id, restart=self.restart)
        if not outcome.staged:
            raise DomainError("operation_failed", f"{name} was not {DONE_WORDS[action]}. {outcome.note}")
        self.results[ext_id] = outcome.note or RESTARTING
        return self.results[ext_id]

    # -- results -------------------------------------------------------------------------------------------------------

    def tell(self, signoff_id, author_id, sentence):
        """Write ``sentence(name)`` on the Signoff as a note by ``author_id`` (the program's worker); the note is the inbox
        event to the agent that proposed it."""
        with self.app.database.transaction() as uow:
            row = uow.repository("signoffs", project_id=None).get(signoff_id)
            if row is None:
                return
            name = self.fields(row["preview_json"]).get("extension") or "The extension"
            self.signoffs.notes.append(InvocationContext(Principal(author_id, "system"), source="worker"), row,
                                       sentence(name), uow)

    def report(self, applied):
        """``finish``'s reporter: the log line, the result for the owner's card, and the result on the Signoff the change
        came from."""
        change.report(applied)
        try:
            found = self.extensions.get(applied["id"])
        except DomainError:
            found = next((item for item in self.extensions.offered if item.id == applied["id"]), None)
        # What the start found in the folder that was moved in (a skill name Qorqbot already has, say): installed, but
        # not usable, and the sentence says why.
        extra = ""
        if (applied["action"] != change.REMOVE and applied["result"]["state"] != change.FAILED and found is not None
                and found.folder is not None and found.status in ("needs_attention", "unsupported") and found.note):
            extra = f" It cannot be used yet: {found.note}"
        name = (found.name if found is not None else None) or applied["id"]
        self.results[applied["id"]] = result_sentence(name, applied) + extra
        if not applied.get("signoff_id"):
            return
        with self.app.database.read() as uow:
            worker = self.programs.authority.worker_id(PROGRAM, uow)
        self.tell(applied["signoff_id"], worker, lambda name: result_sentence(name, applied) + extra)


def result_sentence(name, applied):
    """The result of a change applied at a start, in one or two sentences."""
    action, result = applied["action"], applied["result"]
    if result["state"] == change.FAILED:
        return f"{name} was not {DONE_WORDS[action]}. {result['note']}"
    if action == change.REMOVE:
        # A removal's note is the only word on its data: deleted in part or not at all. Without one it was deleted.
        if applied["delete_data"] and not result["note"]:
            return f"Removed {name}. Its private data was deleted."
        text = f"Removed {name}."
    elif action == change.UPDATE:
        text = f"Updated {name} to {applied['version']}. Qorqbot restarted."
    else:
        text = f"Installed {name} {applied['version']}. Qorqbot restarted."
    return f"{text} {result['note']}" if result["note"] else text


class ChangeProgram:
    """The job behind an approved change: prove it comes from exactly that approved Signoff, then stage it."""

    def __init__(self, tools):
        self.tools = tools

    def run(self, control, args):
        from qorqut.background.approved_send import approved_send_plan
        job = control.job
        operation = REMOVE_TOOL if args["action"] == change.REMOVE else INSTALL_TOOL
        denied = DomainError("forbidden", "This extension change is not backed by an approved owner Signoff.")
        if (job.get("definition_ref_json") or {}).get(MARKER) != {"operation": operation,
                                                                    "agent_id": job.get("initiated_by_principal_id")}:
            raise denied
        with control.service.database.read() as uow:
            approved_send_plan(job, args, operation, uow)
            signoff_id = uow.connection.execute(_APPLIED_SIGNOFF, (operation, job["id"])).fetchone()[0]
        if self.done(job, signoff_id):
            return ProgramResult(data={"operation": operation, "staged": True})
        control.check()
        paths, asked = self.tools.paths, job["initiated_by_principal_id"]
        if args["action"] == change.REMOVE:
            outcome = change.stage_removal(paths, args["extension"], requested_by=asked, signoff_id=signoff_id,
                                           delete_data=args["delete_data"], restart=self.tools.restart)
        else:
            bound = extension_index.Bundle(args["platform"], args["url"], args["sha256"], args["size"])
            outcome = change.stage(paths, SimpleNamespace(id=args["extension"], version=args["version"]), bound,
                                   requested_by=asked, signoff_id=signoff_id, restart=self.tools.restart)
        if not outcome.staged:
            self.tools.tell(signoff_id, job["principal_id"],
                            lambda name: f"{name} was not {DONE_WORDS[args['action']]}. {outcome.note}")
            raise DomainError("operation_failed", outcome.note)
        self.tools.results[args["extension"]] = outcome.note or RESTARTING
        if outcome.note:
            self.tools.tell(signoff_id, job["principal_id"], lambda name: outcome.note)
        return ProgramResult(data={"operation": operation, "staged": True})

    def done(self, job, signoff_id):
        """Whether an earlier run of this job already staged it: its change still waits for the restart, or it was applied
        or failed and its result is written on the Signoff (only this program's worker writes notes there). A run of the
        job again after the restart it asked for then stages nothing."""
        waiting = change.pending(self.tools.paths)
        if waiting is not None and waiting["signoff_id"] == signoff_id:
            return True
        with self.tools.app.database.read() as uow:
            return uow.connection.execute("SELECT 1 FROM signoff_notes WHERE signoff_id=? AND created_by=? LIMIT 1",
                                          (signoff_id, job["principal_id"])).fetchone() is not None
