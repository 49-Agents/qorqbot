"""The integrated-UI domain: registration, immutable accepted code, versioned values, views and notifications.

One Project owns each UI. Everything below is reached through the existing authenticated ``Application.invoke`` path:
each operation is a project-scoped ``OperationDescriptor`` whose handlers use the shared ``PlanBuilder``/prepared
effect model, the existing idempotency receipts (the transport's ``expected_revision`` and ``idempotency_key`` pass
through untouched) and the ordinary unit of work. Reads select the latest value of each key at or before a revision;
writes append one version per changed key and one ordered event, and advance the data revision once. Accepted code is
an immutable stored document: serving never re-reads the authored file.

A code activation and a data commit move their own counters with an explicit check-and-set in the applying
transaction; a metadata edit uses the repository's ``metadata_revision`` CAS. The per-UI event sequence is the
concurrency guard for history appends, so competing tabs conflict instead of silently duplicating or reordering
events. An explicit notification is deduplicated by its semantic key and its delivery receipt is stored on the event
row; the recipients themselves, Task authorization and inbox formatting belong to the injected ``notifier`` and its
``notifications`` implementation. Without that callback an explicit notify fails with ``operation_unavailable``.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import (PlanBuilder, apply_records, page, page_position, ref, rows, schema,
                                                 timestamp, utc_clock)
from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash, new_id, plain
from qorqut_operations.storage.repositories import decode_row

from bazarlyk.agents.global_scope import active_business_participant

from . import limits, source
from .schema import storage_contribution

VIEWER_URL = "/uis/view?ui_id={}"
NOTIFY_OPERATION = "uis_notify"
COMMIT_EVENT = "data_commit"

UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_ID = {"type": "string", "format": "uuid"}
_TITLE = {"type": "string", "minLength": 1, "maxLength": limits.MAX_TITLE_CHARS, "pattern": r"\S"}
_KEY = {"type": "string", "minLength": 1, "maxLength": limits.MAX_KEY_CHARS}
_EVENT_TYPE = {"type": "string", "minLength": 1, "maxLength": limits.MAX_EVENT_TYPE_CHARS, "pattern": r"\S"}
_CURSOR = {"type": "string", "maxLength": 2000}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": limits.MAX_PAGE}
_REVISION = {"type": "integer", "minimum": 1}
_SOURCE_PATH = {"type": "string", "minLength": 1, "maxLength": source.MAX_SOURCE_PATH_CHARS}
_NOTIFICATION_KEY = {"type": "string", "minLength": 1, "maxLength": limits.MAX_NOTIFICATION_KEY_CHARS}
_TASK = {"type": ["string", "null"], "format": "uuid"}
_EVENT = {"type": "object", "properties": {"type": _EVENT_TYPE, "data": {}}, "required": ["type"],
          "additionalProperties": False}
_NOTIFY = {"type": "object",
           "properties": {"type": _EVENT_TYPE, "data": {}, "notification_key": _NOTIFICATION_KEY},
           "required": ["type"], "additionalProperties": False}

_UI = {"type": "object", "additionalProperties": False,
       "required": ["id", "business_id", "title", "slug", "source_path", "creator_id", "task_id", "archived",
                    "metadata_revision", "code_revision", "data_revision", "updated_at", "viewer_url", "can_notify"],
       "properties": {"id": _ID, "business_id": _ID, "title": {"type": "string"}, "slug": {"type": "string"},
                      "source_path": {"type": "string"}, "creator_id": _ID,
                      "task_id": {"type": ["string", "null"]}, "archived": {"type": "boolean"},
                      "metadata_revision": _REVISION, "code_revision": _REVISION, "data_revision": _REVISION,
                      "updated_at": {"type": "string"}, "viewer_url": {"type": "string"},
                      "can_notify": {"type": "boolean"}}}
_CODE = {"type": "object", "additionalProperties": False,
         "required": ["revision", "sha256", "api_version"],
         "properties": {"revision": _REVISION, "sha256": {"type": "string"}, "api_version": _REVISION,
                        "html": {"type": "string"}}}
_EVENT_OUT = {"type": "object", "additionalProperties": False,
              "required": ["id", "sequence", "ui_id", "business_id", "type", "data", "actor_id", "code_revision",
                           "observed_revision", "data_revision", "task_id", "created_at"],
              "properties": {"id": {"type": "string"}, "sequence": _REVISION, "ui_id": _ID, "business_id": _ID,
                             "type": {"type": "string"}, "data": {}, "actor_id": _ID, "code_revision": _REVISION,
                             "observed_revision": _REVISION, "data_revision": _REVISION,
                             "task_id": {"type": ["string", "null"]}, "created_at": {"type": "string"}}}
_NOTIFICATION = {"type": "object", "additionalProperties": False,
                 "required": ["event_id", "inbox_event_id", "delivery_count", "status"],
                 "properties": {"event_id": {"type": "string"}, "inbox_event_id": {"type": ["integer", "null"]},
                                "delivery_count": {"type": "integer", "minimum": 0},
                                "status": {"enum": ["queued", "no_recipients"]}}}
_RECEIPT = {"type": "object", "additionalProperties": False,
            "required": ["ui_id", "data_revision", "event_id", "notification"],
            "properties": {"ui_id": _ID, "data_revision": _REVISION,
                           "event_id": {"type": ["string", "null"]},
                           "notification": {"anyOf": [{"type": "null"}, _NOTIFICATION]}}}
_NEXT = {"type": ["string", "null"]}
_TEMPLATE = {"type": "object", "additionalProperties": False,
             "required": ["id", "title", "description", "source_directory", "entry_file"],
             "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                            "description": {"type": "string"}, "source_directory": {"type": "string"},
                            "entry_file": {"type": "string"}}}


def _object(properties=None, required=()):
    return schema(properties or {}, required)


class IntegratedUIService:
    """The project-scoped integrated-UI operations and the helpers the owner bootstrap and the inbox hook share."""

    def __init__(self, application, paths, *, clock=None, notifier=None):
        self.application = application
        self.paths = paths
        self.clock = clock or utc_clock
        # An injected synchronous callback, executed inside the caller's write transaction: (ctx, ui, event, uow) ->
        # {"inbox_event_id": int|None, "delivery_count": int}. Absent means explicit notify is unavailable.
        self.notifier = notifier
        # Installed template resources; a test may name an explicit package directory before the first call.
        self.template_root = None

    # -- assembly -------------------------------------------------------------------------------------------------

    def register(self, registry) -> "IntegratedUIService":
        """Install the thirteen contracted operations on the open registry."""
        for descriptor in self.descriptors():
            registry.register(descriptor)
        return self

    def descriptors(self) -> tuple[OperationDescriptor, ...]:
        scope = dict(scope_kinds=frozenset({"business"}), supports_signoff=False)
        exposed = dict(exposure=frozenset({"mcp", "dashboard"}))
        owner = dict(exposure=frozenset({"dashboard"}), owner_only=True)
        replay = self.validate_replay
        return (
            OperationDescriptor("uis_list",
                _object({"task_id": _ID, "archived": {"type": "boolean"}, "cursor": _CURSOR, "limit": _LIMIT}),
                {"type": "object", "required": ["items", "next_cursor"],
                 "properties": {"items": {"type": "array", "items": _UI}, "next_cursor": _NEXT}},
                **scope, **exposed, effect_kind="read", replay_validator=replay,
                description="List this project's saved integrated UIs (active by default; archived=true lists archived "
                            "ones) newest first, optionally only those associated with one Task. UI source, saved data "
                            "and history are project-shared; a viewer link is /uis/view?ui_id=<UUID>.",
                read=self.read_list),
            OperationDescriptor("uis_get",
                _object({"ui_id": _ID, "include_code": {"type": "boolean"}}, ("ui_id",)),
                {"type": "object", "required": ["ui"], "properties": {"ui": _UI, "code": _CODE}},
                **scope, **exposed, effect_kind="read", replay_validator=replay,
                description="Read one integrated UI's metadata and revisions, optionally with the exact accepted "
                            "executable document (include_code). A raw UUID never grants access; the Project's current "
                            "authorization applies.",
                read=self.read_get),
            OperationDescriptor("uis_templates", _object(),
                {"type": "object", "required": ["items"],
                 "properties": {"items": {"type": "array", "items": _TEMPLATE}}},
                **scope, **exposed, effect_kind="read", replay_validator=replay,
                description="List the installed starting templates (review queue, form, report) with "
                            "the absolute directory and entry file an agent copies into this project's authored UI "
                            "directory before registering.",
                read=self.read_templates),
            OperationDescriptor("uis_register",
                _object({"title": _TITLE, "source_path": _SOURCE_PATH, "task_id": _TASK}, ("title", "source_path")),
                {"type": "object", "required": ["ui"], "properties": {"ui": _UI}},
                **scope, **exposed, replay_validator=replay,
                description=f"Register one authored entry ('{source.DIRECTORY}/<slug>/{source.ENTRY_FILE}' inside "
                            "this project) as a saved UI. The exact UTF-8 document is read safely, stored as immutable "
                            "code version 1 and served from then on; the live file is never re-read for serving. "
                            "Optional task_id associates an existing Task in the same project (required for "
                            "notifications). Reuse the idempotency_key on retry.",
                prepare=self.prepare_register, apply=self.apply_register, validate_targets=self.validate_register),
            OperationDescriptor("uis_update",
                _object({"ui_id": _ID, "title": _TITLE, "task_id": _TASK, "archived": {"type": "boolean"}},
                        ("ui_id",)),
                {"type": "object", "required": ["ui"], "properties": {"ui": _UI}},
                **scope, **exposed, replay_validator=replay,
                description="Update a UI's title, Task association (task_id:null unlinks) or archive flag with the "
                            "current metadata_revision. Omitted fields are unchanged. Archiving keeps source, code, "
                            "data and history readable but refuses writes and notifications; an authorized unarchive "
                            "(archived:false) restores use. Never resets saved data.",
                prepare=self.prepare_update, apply=self.apply_update, validate_targets=self.validate_update),
            OperationDescriptor("uis_code_set",
                _object({"ui_id": _ID, "expected_code_revision": _REVISION}, ("ui_id", "expected_code_revision")),
                {"type": "object", "required": ["ui", "code"], "properties": {"ui": _UI, "code": _CODE}},
                **scope, **exposed, replay_validator=replay,
                description="Read the project's authored entry again and activate it as the next immutable code "
                            "version, checked against the current code_revision and the supplied metadata revision. "
                            "Data is never reset; frames of the older version receive a conflict on their next "
                            "mutation. Identical content returns the current version without a new one.",
                prepare=self.prepare_code_set, apply=self.apply_code_set, validate_targets=self.validate_code_set),
            OperationDescriptor("uis_data_read",
                _object({"ui_id": _ID, "keys": {"type": "array", "items": _KEY, "maxItems": limits.MAX_CHANGED_KEYS},
                         "prefix": {"type": "string", "maxLength": limits.MAX_KEY_CHARS},
                         "revision": _REVISION, "cursor": _CURSOR, "limit": _LIMIT}, ("ui_id",)),
                {"type": "object", "required": ["ui_id", "revision", "values", "next_cursor"],
                 "properties": {"ui_id": _ID, "revision": _REVISION, "values": {"type": "object"},
                                "next_cursor": _NEXT}},
                **scope, **exposed, effect_kind="read", replay_validator=replay,
                description="Read saved values at a revision (default: the current one): named keys, every key with a "
                            "prefix, or all keys in bounded pages. A key removed earlier is absent; its earlier "
                            "versions remain readable at their revisions. The cursor is pinned to the revision it was "
                            "issued for.",
                read=self.read_data),
            OperationDescriptor("uis_data_write",
                _object({"ui_id": _ID, "code_revision": _REVISION,
                         "set": {"type": "object", "propertyNames": _KEY, "additionalProperties": {}},
                         "remove": {"type": "array", "items": _KEY, "maxItems": limits.MAX_CHANGED_KEYS},
                         "event": _EVENT, "notify": _NOTIFY}, ("ui_id", "code_revision")),
                _RECEIPT,
                **scope, **exposed, replay_validator=replay,
                description="Save a bounded group of complete values (set) and/or removals (remove) in one atomic "
                            "commit at the current data revision. The commit advances the data revision once and "
                            "records one change event; an unchanged value is not rewritten. Optional event adds "
                            "application meaning to that change record; optional notify (requiring an associated "
                            "Task) delivers the committed revision in the same transaction. A stale data revision or "
                            "an older frame's code_revision is refused; nothing is rebased.",
                prepare=self.prepare_data_write, apply=self.apply_data_write,
                validate_targets=self.validate_data_write),
            OperationDescriptor("uis_emit",
                _object({"ui_id": _ID, "code_revision": _REVISION, "type": _EVENT_TYPE, "data": {},
                         "observed_revision": _REVISION},
                        ("ui_id", "code_revision", "type", "observed_revision")),
                _RECEIPT,
                **scope, **exposed, replay_validator=replay,
                description="Record one semantic history event (for example a completion) without changing saved "
                            "data, naming the data revision that was observed. It does not notify anyone; use "
                            "uis_notify for that.",
                prepare=self.prepare_emit, apply=self.apply_emit, validate_targets=self.validate_emit),
            OperationDescriptor("uis_notify",
                _object({"ui_id": _ID, "code_revision": _REVISION, "type": _EVENT_TYPE, "data": {},
                         "revision": _REVISION, "notification_key": _NOTIFICATION_KEY},
                        ("ui_id", "code_revision", "type", "revision")),
                _RECEIPT,
                **scope, **exposed, replay_validator=replay,
                description="Notify the UI creator and the associated Task's eligible followers about an already "
                            "saved revision. The deduplication key defaults to UI/type/revision, so reopening a "
                            "completed UI does not repeat its notification; an explicit key allows a deliberate "
                            "repeat and is refused with different content. Requires an associated Task.",
                prepare=self.prepare_notify, apply=self.apply_notify, validate_targets=self.validate_notify),
            OperationDescriptor("uis_events",
                _object({"ui_id": _ID, "cursor": _CURSOR, "limit": _LIMIT}, ("ui_id",)),
                {"type": "object", "required": ["items", "next_cursor"],
                 "properties": {"items": {"type": "array", "items": _EVENT_OUT}, "next_cursor": _NEXT}},
                **scope, **exposed, effect_kind="read", replay_validator=replay,
                description="Read one UI's ordered history in stable pages: changes, semantic events and explicit "
                            "notification receipts, with the actor, code/data revision and Task association at the "
                            "time. Archives remain readable.",
                read=self.read_events),
            OperationDescriptor("uis_view_get", _object({"ui_id": _ID}, ("ui_id",)),
                {"type": "object", "required": ["state"], "properties": {"state": {"type": "object"}}},
                **scope, **owner, effect_kind="read", replay_validator=replay,
                description="Owner: read this viewer's saved presentation state (current item, sorting, expanded "
                            "sections) for one UI. Presentation only: no decision history and no data revision.",
                read=self.read_view),
            OperationDescriptor("uis_view_set",
                _object({"ui_id": _ID, "code_revision": _REVISION, "state": {"type": "object"}},
                        ("ui_id", "code_revision", "state")),
                {"type": "object", "required": ["state"], "properties": {"state": {"type": "object"}}},
                **scope, **owner, replay_validator=replay,
                description="Owner: save this viewer's bounded presentation state for one UI (latest write wins). It "
                            "never advances the data revision, records no event and never wakes an agent; an older "
                            "frame's code_revision is refused.",
                prepare=self.prepare_view_set, apply=self.apply_view_set,
                validate_targets=self.validate_view_set),
        )

    def storage_contribution(self):
        return storage_contribution()

    # -- shared reads ---------------------------------------------------------------------------------------------

    @staticmethod
    def _identity(value: Any, *, field: str = "ui_id") -> str:
        try:
            if not isinstance(value, str) or str(UUID(value)) != value.lower():
                raise ValueError
            return value.lower()
        except (ValueError, AttributeError, TypeError) as exc:
            raise DomainError("validation_error", "A canonical UUID is required",
                              field_errors={field: "must be a UUID"}) from exc

    def _row(self, ctx, ui_id, uow, *, missing="not_found"):
        """The UI row inside the caller's selected project; a UUID from another project is simply not found."""
        business = ctx.business_id
        if business is None:
            raise DomainError("scope_required", "Select an authorized project")
        row = uow.repository("integrated_uis", business_id=business).get(self._identity(ui_id))
        if row is None:
            raise DomainError(missing, "This integrated UI is unavailable")
        return row

    def ui_row(self, ctx, ui_id, uow):
        return self._row(ctx, ui_id, uow)

    def can_read(self, ctx, ui_id, uow) -> bool:
        """Whether this caller may read the UI right now.

        Current project reach and the read operation's own authorization are both required (an explicit deny wins);
        resolving a raw UUID alone never grants anything. No mutation authority is consulted or granted here.
        """
        try:
            return self._readable(ctx, ui_id, uow)
        except DomainError:
            return False

    def _readable(self, ctx, ui_id, uow) -> bool:
        if not isinstance(ui_id, str):
            return False
        canon = ui_id.lower()
        if not UUID_TEXT.fullmatch(canon):
            return False
        found = uow.connection.execute("SELECT business_id FROM integrated_uis WHERE id=?", (canon,)).fetchone()
        if found is None:
            return False
        business = found["business_id"]
        if ctx.business_id is None:
            # Only the installation owner may resolve an unselected context; an agent or another human never does.
            installation = uow.repository("installation").get("service")
            if (ctx.principal_kind != "human" or installation is None
                    or installation["owner_principal_id"] != ctx.principal_id):
                return False
            selected = replace(ctx, business_id=business)
        elif ctx.business_id == business:
            selected = ctx
        else:
            return False
        if not active_business_participant(uow.connection, business, ctx.principal_id):
            return False
        try:
            self.application.authorize(selected, self.application.registry.get("uis_get"), (), uow,
                                       namespace="business:" + business)
        except DomainError:
            return False
        return True

    def get_authorized(self, ctx, ui_id, uow):
        """The UI DTO for this project, or the ordinary scoped ``not_found``/server error."""
        row = self._row(ctx, ui_id, uow)
        return self.ui_dto(row, self._notify_authorized(ctx, row["business_id"], uow))

    def _notify_authorized(self, ctx, business, uow) -> bool:
        """The current caller's own authority to request a notification for this project (never a write grant)."""
        if business is None or ctx.business_id != business:
            return False
        if not active_business_participant(uow.connection, business, ctx.principal_id):
            return False
        try:
            self.application.authorize(ctx, self.application.registry.get(NOTIFY_OPERATION), (), uow,
                                       namespace="business:" + business)
        except DomainError:
            return False
        return True

    def validate_replay(self, ctx, arguments, replay, uow):
        """A saved idempotency receipt never outlives the Project access it was issued under."""
        business = ctx.business_id
        if business is None or business not in ctx.allowed_business_ids:
            raise DomainError("forbidden", "Project is outside the principal's scope")
        if not active_business_participant(uow.connection, business, ctx.principal_id):
            raise DomainError("forbidden", "Project access changed")
        ui_id = arguments.get("ui_id")
        if ui_id is not None and uow.repository("integrated_uis", business_id=business).get(
                self._identity(ui_id)) is None:
            raise DomainError("not_found", "This integrated UI is unavailable")

    # -- project, source and code ---------------------------------------------------------------------------------

    def project(self, uow, business_id):
        row = uow.repository("businesses").get(business_id) if business_id else None
        if row is None:
            raise DomainError("not_found", "The project is unavailable")
        return row

    def _active_project(self, ctx, uow):
        project = self.project(uow, ctx.business_id)
        if project["archived_at"] is not None:
            raise DomainError("forbidden", "The project is archived; its authored UIs are read-only")
        return project

    def _task(self, uow, business_id, task_id):
        """A nullable Task association: one existing Task of this same project, or nothing."""
        if task_id is None:
            return None
        task = uow.repository("tasks", business_id=business_id).get(self._identity(task_id, field="task_id"))
        if task is None:
            raise DomainError("invalid_argument", "The associated Task does not exist in this project",
                              field_errors={"task_id": "an existing Task in this project is required"})
        return task

    def _code(self, uow, ui, *, revision=None):
        revision = ui["code_revision"] if revision is None else revision
        found = uow.connection.execute(
            "SELECT revision,sha256,api_version,html,byte_length,actor_id,created_at FROM integrated_ui_code "
            "WHERE business_id=? AND ui_id=? AND revision=?",
            (ui["business_id"], ui["id"], revision)).fetchone()
        if found is None:
            raise DomainError("not_found", "The accepted code for this UI is unavailable")
        return dict(found)

    def _capture(self, project, relative):
        return source.capture(self.paths.business_root(project["slug"]), relative, limit=limits.MAX_SOURCE_BYTES)

    def _require_writable(self, ctx, ui, uow):
        """Every mutation but an authorized unarchive refuses an archived UI; the Project must be active too."""
        self._active_project(ctx, uow)
        if ui["archived"]:
            raise DomainError("ui_archived", "This UI is archived; unarchive it before writing")

    @staticmethod
    def _require_code(ui, code_revision):
        if code_revision != ui["code_revision"]:
            raise DomainError("code_revision_conflict",
                              "This frame serves an older code version; reload the UI",
                              current_revision=ui["code_revision"],
                              details={"current_code_revision": ui["code_revision"]})

    @staticmethod
    def _require_metadata(ui, expected):
        if expected is None:
            raise DomainError("validation_error", "expected_revision is required: the metadata revision you read")
        if ui["metadata_revision"] != expected:
            raise DomainError("revision_conflict", "The UI metadata changed; reload it",
                              current_revision=ui["metadata_revision"])

    @staticmethod
    def _require_data(ui, expected):
        if expected is None:
            raise DomainError("validation_error", "expected_revision is required: the data revision you read")
        if ui["data_revision"] != expected:
            raise DomainError("revision_conflict", "Saved data changed; reload the UI",
                              current_revision=ui["data_revision"])

    # -- DTOs -----------------------------------------------------------------------------------------------------

    @staticmethod
    def ui_dto(row, notify_authorized=False):
        return {"id": row["id"], "business_id": row["business_id"], "title": row["title"], "slug": row["slug"],
                "source_path": row["source_path"], "creator_id": row["creator_id"], "task_id": row["task_id"],
                "archived": bool(row["archived"]), "metadata_revision": row["metadata_revision"],
                "code_revision": row["code_revision"], "data_revision": row["data_revision"],
                "updated_at": row["updated_at"], "viewer_url": VIEWER_URL.format(row["id"]),
                "can_notify": bool(notify_authorized and row["task_id"] and not row["archived"])}

    @staticmethod
    def code_dto(row, *, include_html=False):
        data = {"revision": row["revision"], "sha256": row["sha256"], "api_version": row["api_version"]}
        if include_html:
            data["html"] = row["html"]
        return data

    @staticmethod
    def event_dto(row):
        return {"id": row["id"], "sequence": row["sequence"], "ui_id": row["ui_id"],
                "business_id": row["business_id"], "type": row["event_type"], "data": row.get("data_json"),
                "actor_id": row["actor_id"], "code_revision": row["code_revision"],
                "observed_revision": row["observed_revision"], "data_revision": row["data_revision"],
                "task_id": row["task_id"], "created_at": row["created_at"]}

    @staticmethod
    def _receipt(ui_id, data_revision, event_id=None, notification=None):
        return {"ui_id": ui_id, "data_revision": data_revision, "event_id": event_id, "notification": notification}

    @staticmethod
    def _stored_notification(row):
        return {"event_id": row["id"], "inbox_event_id": row["inbox_event_id"],
                "delivery_count": row["delivery_count"], "status": row["notification_status"]}

    # -- reads ----------------------------------------------------------------------------------------------------

    def read_list(self, ctx, arguments, options, uow):
        business = ctx.business_id
        notify = self._notify_authorized(ctx, business, uow)
        clauses, parameters = ["archived=?"], [1 if arguments.get("archived", False) else 0]
        if arguments.get("task_id") is not None:
            clauses.append("task_id=?")
            parameters.append(self._identity(arguments["task_id"], field="task_id"))
        found = rows(uow, "integrated_uis", business, where=" AND ".join(clauses), parameters=parameters)
        selected, next_cursor = page(found, arguments, business, "uis_list",
                                     key=lambda row: (row["updated_at"], row["id"]))
        return {"items": [self.ui_dto(row, notify) for row in selected], "next_cursor": next_cursor}

    def read_get(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        data = {"ui": self.ui_dto(ui, self._notify_authorized(ctx, ui["business_id"], uow))}
        if arguments.get("include_code", False):
            data["code"] = self.code_dto(self._code(uow, ui), include_html=True)
        return data

    def read_templates(self, ctx, arguments, options, uow):
        return {"items": source.discover_templates(self.template_root)}

    def _values_at(self, uow, ui, revision, *, keys=None, prefix=None, after=None, limit=None):
        """Latest value of each selected key at or before ``revision``; tombstones and absent keys are omitted."""
        connection, business = uow.connection, ui["business_id"]
        parameters = [business, ui["id"], revision]
        condition = ""
        if keys is not None:
            condition = " AND key IN (" + ",".join("?" for _ in keys) + ")"
            parameters.extend(keys)
        elif prefix is not None:
            condition = " AND key >= ? AND key < ?"
            parameters.extend([prefix, prefix + "\U0010ffff"])
        tail = ""
        if after is not None:
            tail = " AND v.key > ?"
        statement = (
            "SELECT v.key AS key, v.value_json AS value_json FROM integrated_ui_values v JOIN ("
            "SELECT key, MAX(data_revision) AS newest FROM integrated_ui_values "
            "WHERE business_id=? AND ui_id=? AND data_revision<=?" + condition + " GROUP BY key) latest "
            "ON latest.key=v.key AND latest.newest=v.data_revision "
            "WHERE v.business_id=? AND v.ui_id=? AND v.tombstone=0" + tail + " ORDER BY v.key")
        values = [*parameters, business, ui["id"]]
        if after is not None:
            values.append(after)
        if limit is not None:
            statement += " LIMIT ?"
            values.append(limit)
        return {row["key"]: json.loads(row["value_json"]) for row in connection.execute(statement, values)}

    def read_data(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        revision = arguments.get("revision", ui["data_revision"])
        if revision > ui["data_revision"]:
            raise limits.invalid("The requested revision does not exist yet", field="revision",
                                 current_revision=ui["data_revision"])
        keys, prefix = arguments.get("keys"), arguments.get("prefix")
        if keys is not None and prefix is not None:
            raise limits.invalid("Read named keys or a prefix, not both", field="prefix")
        if keys is not None:
            if arguments.get("cursor"):
                raise limits.invalid("A named-key read does not paginate", field="cursor")
            wanted = sorted({limits.data_key(key) for key in keys})
            return {"ui_id": ui["id"], "revision": revision,
                    "values": self._values_at(uow, ui, revision, keys=wanted), "next_cursor": None}
        # The binding names the revision actually used, so a cursor issued against the current revision cannot
        # silently continue at a newer one after a commit; reusing it requires naming the revision it was issued for.
        binding, after = page_position({**arguments, "revision": revision}, ui["business_id"], "uis_data_read")
        if after is not None and (len(after) != 1 or not isinstance(after[0], str)):
            raise DomainError("validation_error", "Invalid or differently scoped read cursor")
        found = self._values_at(uow, ui, revision, prefix="" if prefix is None else prefix,
                                after=None if after is None else after[0], limit=limits.page_limit(arguments) + 1)
        items = sorted(found.items())
        next_cursor = None
        limit = limits.page_limit(arguments)
        if len(items) > limit:
            items = items[:limit]
            next_cursor = base64.urlsafe_b64encode(canonical_bytes(
                {"binding": binding, "after": [items[-1][0]]})).decode("ascii")
        return {"ui_id": ui["id"], "revision": revision, "values": dict(items), "next_cursor": next_cursor}

    def read_events(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        business, limit = ui["business_id"], limits.page_limit(arguments)
        binding, after = page_position(arguments, business, "uis_events")
        sequence = 0
        if after is not None:
            if len(after) != 1 or type(after[0]) is not int or after[0] < 1:
                raise DomainError("validation_error", "Invalid or differently scoped event cursor")
            sequence = after[0]
        found = uow.connection.execute(
            "SELECT * FROM integrated_ui_events WHERE business_id=? AND ui_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?", (business, ui["id"], sequence, limit + 1)).fetchall()
        items = [decode_row(row) for row in found[:limit]]
        next_cursor = None
        if len(found) > limit:
            next_cursor = base64.urlsafe_b64encode(canonical_bytes(
                {"binding": binding, "after": [items[-1]["sequence"]]})).decode("ascii")
        return {"items": [self.event_dto(row) for row in items], "next_cursor": next_cursor}

    def read_view(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        found = uow.connection.execute(
            "SELECT state_json FROM integrated_ui_views WHERE business_id=? AND ui_id=? AND principal_id=?",
            (ui["business_id"], ui["id"], ctx.principal_id)).fetchone()
        return {"state": json.loads(found["state_json"]) if found else {}}

    # -- registration and metadata --------------------------------------------------------------------------------

    def prepare_register(self, ctx, arguments, options, uow):
        project = self._active_project(ctx, uow)
        business = ctx.business_id
        slug, relative = source.parse_source_path(arguments["source_path"])
        self._task(uow, business, arguments.get("task_id"))
        existing = uow.connection.execute(
            "SELECT id FROM integrated_uis WHERE business_id=? AND source_path=?", (business, relative)).fetchone()
        if existing is not None:
            raise limits.invalid("This source directory is already registered", field="source_path",
                                 ui_id=existing["id"])
        document = self._capture(project, relative)
        title = limits.title(arguments["title"])
        builder = PlanBuilder(ctx, "uis_register", arguments, "Register integrated UI", clock=self.clock)
        ui_id = new_id()
        builder.record("integrated_uis", ui_id, None, {
            "id": ui_id, "title": title, "slug": slug, "source_path": relative,
            "creator_id": ctx.principal_id, "task_id": arguments.get("task_id"), "archived": 0,
            "metadata_revision": 1, "code_revision": 1, "data_revision": 1, "event_sequence": 1,
            "created_at": builder.at, "updated_at": builder.at})
        builder.record("integrated_ui_code", {"ui_id": ui_id, "revision": 1}, None, {
            "ui_id": ui_id, "revision": 1, "sha256": document.sha256, "api_version": 1,
            "html": document.html, "byte_length": document.byte_length, "actor_id": ctx.principal_id,
            "created_at": builder.at})
        self._plan_event(builder, ctx, ui_id, 1, "registered",
                         {"slug": slug, "source_path": relative, "code_sha256": document.sha256,
                          "byte_length": document.byte_length},
                         observed_revision=1, data_revision=1, code_revision=1,
                         task_id=arguments.get("task_id"))
        return builder.build()

    def validate_register(self, ctx, plan, uow):
        project = self._active_project(ctx, uow)
        _slug, relative = source.parse_source_path(plan.arguments["source_path"])
        self._task(uow, ctx.business_id, plan.arguments.get("task_id"))
        self._capture(project, relative)

    def apply_register(self, ctx, plan, uow):
        try:
            apply_records(ctx, plan, uow)
        except sqlite3.IntegrityError as exc:
            raise limits.invalid("This source directory is already registered", field="source_path") from exc
        ui_id = next(change.resource.resource_id for change in plan.changes
                     if change.resource.resource_type == "integrated_uis")
        ui = self._row(ctx, ui_id, uow)
        return {"ui": self.ui_dto(ui, self._notify_authorized(ctx, ui["business_id"], uow))}

    def prepare_update(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        self._active_project(ctx, uow)
        if not set(arguments) & {"title", "task_id", "archived"}:
            raise limits.invalid("Name a title, a Task association or the archive flag to update")
        if ui["archived"] and arguments.get("archived") is not False:
            raise DomainError("ui_archived", "This UI is archived; only an authorized unarchive may change it")
        self._require_metadata(ui, options.expected_revision)
        changes = {}
        if "title" in arguments:
            changes["title"] = limits.title(arguments["title"])
        if "task_id" in arguments:
            self._task(uow, ui["business_id"], arguments["task_id"])
            changes["task_id"] = arguments["task_id"]
        if "archived" in arguments:
            changes["archived"] = 1 if arguments["archived"] else 0
        effective = {key: value for key, value in changes.items() if ui[key] != value}
        builder = PlanBuilder(ctx, "uis_update", arguments, "Update integrated UI", clock=self.clock)
        builder.expect(ref("integrated_uis", ui["id"], ui["business_id"]), ui)
        if not effective:
            return builder.build()
        sequence = ui["event_sequence"] + 1
        builder.record("integrated_uis", ui["id"], ui, {**effective, "event_sequence": sequence})
        self._plan_event(builder, ctx, ui["id"], sequence, "metadata_updated", effective,
                         observed_revision=ui["data_revision"], data_revision=ui["data_revision"],
                         code_revision=ui["code_revision"],
                         task_id=effective.get("task_id", ui["task_id"]))
        return builder.build()

    def validate_update(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._active_project(ctx, uow)
        if ui["archived"] and plan.arguments.get("archived") is not False:
            raise DomainError("ui_archived", "This UI is archived; only an authorized unarchive may change it")
        if "task_id" in plan.arguments:
            self._task(uow, ui["business_id"], plan.arguments["task_id"])

    def apply_update(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._active_project(ctx, uow)
        if ui["archived"] and plan.arguments.get("archived") is not False:
            raise DomainError("ui_archived", "This UI is archived; only an authorized unarchive may change it")
        if plan.changes:
            apply_records(ctx, plan, uow)
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        return {"ui": self.ui_dto(ui, self._notify_authorized(ctx, ui["business_id"], uow))}

    # -- code activation ------------------------------------------------------------------------------------------

    def prepare_code_set(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_metadata(ui, options.expected_revision)
        if arguments["expected_code_revision"] != ui["code_revision"]:
            raise DomainError("code_revision_conflict", "Accepted code changed; reload the UI",
                              current_revision=ui["code_revision"],
                              details={"current_code_revision": ui["code_revision"]})
        project = self.project(uow, ui["business_id"])
        document = self._capture(project, ui["source_path"])
        builder = PlanBuilder(ctx, "uis_code_set", arguments, "Activate accepted UI code", clock=self.clock)
        builder.expect(ref("integrated_uis", ui["id"], ui["business_id"]), ui)
        if document.sha256 == self._code(uow, ui)["sha256"]:
            return builder.build()
        revision, sequence = ui["code_revision"] + 1, ui["event_sequence"] + 1
        builder.record("integrated_ui_code", {"ui_id": ui["id"], "revision": revision}, None, {
            "ui_id": ui["id"], "revision": revision, "sha256": document.sha256, "api_version": 1,
            "html": document.html, "byte_length": document.byte_length, "actor_id": ctx.principal_id,
            "created_at": builder.at})
        self._plan_event(builder, ctx, ui["id"], sequence, "code_activated",
                         {"previous_code_revision": ui["code_revision"], "sha256": document.sha256,
                          "byte_length": document.byte_length},
                         observed_revision=ui["data_revision"], data_revision=ui["data_revision"],
                         code_revision=revision, task_id=ui["task_id"])
        return builder.build()

    def validate_code_set(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._capture(self.project(uow, ui["business_id"]), ui["source_path"])

    def apply_code_set(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        planned = self._planned_events(plan)
        if planned:
            self._advance(ui, {"code_revision": planned[0]["code_revision"], "event_sequence": planned[-1]["sequence"]},
                          expected={"code_revision": plan.arguments["expected_code_revision"],
                                    "event_sequence": planned[0]["sequence"] - 1}, uow=uow)
            apply_records(ctx, plan, uow)
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        return {"ui": self.ui_dto(ui, self._notify_authorized(ctx, ui["business_id"], uow)),
                "code": self.code_dto(self._code(uow, ui))}

    # -- data -----------------------------------------------------------------------------------------------------

    def prepare_data_write(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, arguments["code_revision"])
        self._require_data(ui, options.expected_revision)
        values, removed = limits.commit(arguments)
        event, notify = arguments.get("event"), arguments.get("notify")
        if event is not None:
            event = {**event, "type": limits.event_type(event["type"], field="event.type"),
                     "data": limits.event_data(event.get("data"), field="event.data")}
        if notify is not None:
            self._require_notification(ui)
            self._require_notifier()
            notify = {**notify, "type": limits.event_type(notify["type"], field="notify.type"),
                      "data": limits.event_data(notify.get("data"), field="notify.data")}
        current = (self._values_at(uow, ui, ui["data_revision"], keys=sorted({*values, *removed}))
                   if values or removed else {})
        changed = {key: value for key, value in values.items()
                   if key not in current or canonical_bytes(current[key]) != canonical_bytes(value)}
        deletions = [key for key in removed if key in current]
        revision = ui["data_revision"] + 1 if changed or deletions else ui["data_revision"]
        builder = PlanBuilder(ctx, "uis_data_write", arguments, "Save integrated UI data", clock=self.clock)
        builder.expect(ref("integrated_uis", ui["id"], ui["business_id"]), ui)
        sequence = ui["event_sequence"]
        commit_event = event is not None or bool(changed) or bool(deletions)
        if commit_event:
            sequence += 1
            self._plan_event(builder, ctx, ui["id"], sequence,
                             (event or {}).get("type", COMMIT_EVENT), (event or {}).get("data"),
                             observed_revision=ui["data_revision"], data_revision=revision,
                             code_revision=ui["code_revision"], task_id=ui["task_id"])
        for key, value in changed.items():
            self._plan_value(builder, ctx, ui, key, revision, value=value)
        for key in deletions:
            self._plan_value(builder, ctx, ui, key, revision, value=None)
        if notify is not None:
            key = self._notification_key(ui, notify, revision)
            payload = self._payload_hash(notify, revision)
            existing = self._notification(uow, ui, key)
            if existing is not None:
                if existing["notification_payload_hash"] != payload:
                    raise DomainError("notification_key_conflict",
                                      "This notification key was already used for different content",
                                      details={"event_id": existing["id"], "notification_key": key})
            else:
                sequence += 1
                self._plan_event(builder, ctx, ui["id"], sequence, notify["type"], notify.get("data"),
                                 observed_revision=revision, data_revision=revision,
                                 code_revision=ui["code_revision"], task_id=ui["task_id"],
                                 notification=(key, payload))
        return builder.build()

    def validate_data_write(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, plan.arguments["code_revision"])
        if plan.arguments.get("notify") is not None:
            self._require_notification(ui)
            self._require_notifier()

    def apply_data_write(self, ctx, plan, uow):
        arguments = plan.arguments
        ui = self._row(ctx, arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, arguments["code_revision"])
        planned = self._planned_events(plan)
        notify = arguments.get("notify")
        if notify is not None:
            self._require_notification(ui)
            self._require_notifier()
        if not planned:
            return self._receipt(ui["id"], ui["data_revision"], None,
                                 None if notify is None else self._duplicate(ctx, ui, notify, ui["data_revision"], uow))
        committed = next((row for row in planned if row.get("notification_key") is None), None)
        notification_event = next((row for row in planned if row.get("notification_key") is not None), None)
        reference = committed or notification_event
        expected, revision = reference["observed_revision"], planned[0]["data_revision"]
        if ui["data_revision"] != expected:
            raise DomainError("revision_conflict", "Saved data changed; reload the UI",
                              current_revision=ui["data_revision"])
        updates = {"event_sequence": planned[-1]["sequence"]}
        if revision != ui["data_revision"]:
            updates.update(data_revision=revision, updated_at=timestamp(self.clock))
        self._advance(ui, updates, expected={"data_revision": expected,
                                             "event_sequence": planned[0]["sequence"] - 1}, uow=uow)
        apply_records(ctx, plan, uow)
        if notification_event is not None:
            notification = self._deliver(ctx, ui, notification_event, uow)
        elif notify is not None:
            notification = self._duplicate(ctx, ui, notify, revision, uow)
        else:
            notification = None
        return self._receipt(ui["id"], revision, None if committed is None else committed["id"], notification)

    # -- events, notifications and views --------------------------------------------------------------------------

    def prepare_emit(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, arguments["code_revision"])
        limits.observed(arguments["observed_revision"], current=ui["data_revision"])
        builder = PlanBuilder(ctx, "uis_emit", arguments, "Record integrated UI event", clock=self.clock)
        builder.expect(ref("integrated_uis", ui["id"], ui["business_id"]), ui)
        self._plan_event(builder, ctx, ui["id"], ui["event_sequence"] + 1,
                         limits.event_type(arguments["type"]),
                         limits.event_data(arguments.get("data")),
                         observed_revision=arguments["observed_revision"], data_revision=ui["data_revision"],
                         code_revision=ui["code_revision"], task_id=ui["task_id"])
        return builder.build()

    def validate_emit(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, plan.arguments["code_revision"])
        limits.observed(plan.arguments["observed_revision"], current=ui["data_revision"])

    def apply_emit(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, plan.arguments["code_revision"])
        planned = self._planned_events(plan)
        self._advance(ui, {"event_sequence": planned[-1]["sequence"]},
                      expected={"data_revision": ui["data_revision"],
                                "event_sequence": planned[0]["sequence"] - 1}, uow=uow)
        apply_records(ctx, plan, uow)
        return self._receipt(ui["id"], ui["data_revision"], planned[0]["id"])

    def prepare_notify(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, arguments["code_revision"])
        self._require_notification(ui)
        self._require_notifier()
        event_type = limits.event_type(arguments["type"])
        data = limits.event_data(arguments.get("data"))
        revision = limits.observed(arguments["revision"], current=ui["data_revision"])
        request = {"type": event_type, "data": data, "notification_key": arguments.get("notification_key")}
        key, payload = self._notification_key(ui, request, revision), self._payload_hash(request, revision)
        builder = PlanBuilder(ctx, "uis_notify", arguments, "Notify about integrated UI revision", clock=self.clock)
        builder.expect(ref("integrated_uis", ui["id"], ui["business_id"]), ui)
        existing = self._notification(uow, ui, key)
        if existing is not None:
            if existing["notification_payload_hash"] != payload:
                raise DomainError("notification_key_conflict",
                                  "This notification key was already used for different content",
                                  details={"event_id": existing["id"], "notification_key": key})
            return builder.build()
        self._plan_event(builder, ctx, ui["id"], ui["event_sequence"] + 1, event_type, data,
                         observed_revision=revision, data_revision=revision, code_revision=ui["code_revision"],
                         task_id=ui["task_id"], notification=(key, payload))
        return builder.build()

    def validate_notify(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, plan.arguments["code_revision"])
        self._require_notification(ui)
        limits.observed(plan.arguments["revision"], current=ui["data_revision"])

    def apply_notify(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, plan.arguments["code_revision"])
        self._require_notification(ui)
        self._require_notifier()
        revision = limits.observed(plan.arguments["revision"], current=ui["data_revision"])
        planned = self._planned_events(plan)
        if not planned:
            request = {"type": plan.arguments["type"], "data": plan.arguments.get("data"),
                       "notification_key": plan.arguments.get("notification_key")}
            existing = self._notification(uow, ui, self._notification_key(ui, request, revision))
            if existing is None:
                raise DomainError("invalid_plan", "The saved notification is no longer available")
            return self._receipt(ui["id"], revision, existing["id"], self._stored_notification(existing))
        self._advance(ui, {"event_sequence": planned[-1]["sequence"]},
                      expected={"data_revision": ui["data_revision"],
                                "event_sequence": planned[0]["sequence"] - 1}, uow=uow)
        apply_records(ctx, plan, uow)
        return self._receipt(ui["id"], revision, planned[0]["id"], self._deliver(ctx, ui, planned[0], uow))

    def prepare_view_set(self, ctx, arguments, options, uow):
        ui = self._row(ctx, arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, arguments["code_revision"])
        limits.view_state(arguments["state"])
        builder = PlanBuilder(ctx, "uis_view_set", arguments, "Save UI presentation state", clock=self.clock)
        builder.expect(ref("integrated_uis", ui["id"], ui["business_id"]), ui)
        return builder.build()

    def validate_view_set(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, plan.arguments["code_revision"])

    def apply_view_set(self, ctx, plan, uow):
        ui = self._row(ctx, plan.arguments["ui_id"], uow)
        self._require_writable(ctx, ui, uow)
        self._require_code(ui, plan.arguments["code_revision"])
        state = limits.view_state(plan.arguments["state"])
        now = timestamp(self.clock)
        # Presentation preferences are latest-write-wins: an upsert, never a revision conflict, and no event.
        uow.connection.execute(
            "INSERT INTO integrated_ui_views(business_id,ui_id,principal_id,state_json,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,1,?,?) ON CONFLICT(business_id,ui_id,principal_id) DO UPDATE SET "
            "state_json=excluded.state_json,revision=revision+1,updated_at=excluded.updated_at",
            (ui["business_id"], ui["id"], ctx.principal_id, canonical_bytes(state).decode("utf-8"), now, now))
        return {"state": state}

    # -- prepared-effect helpers ----------------------------------------------------------------------------------

    def _plan_event(self, builder, ctx, ui_id, sequence, event_type, data, *, observed_revision, data_revision,
                    code_revision, task_id, notification=None):
        """Append one prepared event row (its resource ID and its own ``id`` value are the same identity)."""
        event_id = new_id()
        values = {"id": event_id, "ui_id": ui_id, "business_id": ctx.business_id, "sequence": sequence,
                  "event_type": event_type, "data_json": data, "actor_id": ctx.principal_id,
                  "code_revision": code_revision, "observed_revision": observed_revision,
                  "data_revision": data_revision, "task_id": task_id, "created_at": builder.at}
        if notification is not None:
            values["notification_key"], values["notification_payload_hash"] = notification
        builder.record("integrated_ui_events", event_id, None, values)
        return event_id

    def _plan_value(self, builder, ctx, ui, key, revision, *, value):
        row = {"ui_id": ui["id"], "key": key, "data_revision": revision, "tombstone": 0 if value is not None else 1,
               "value_json": None if value is None else canonical_bytes(value).decode("utf-8"),
               "actor_id": ctx.principal_id, "created_at": builder.at}
        builder.record("integrated_ui_values", {"ui_id": ui["id"], "key": key, "data_revision": revision}, None, row)

    @staticmethod
    def _planned_events(plan):
        """The prepared event rows of a plan in sequence order (each carrying its own project scope)."""
        found = [{**plain(change.values), "business_id": change.resource.business_id}
                 for change in plan.changes if change.resource.resource_type == "integrated_ui_events"]
        return sorted(found, key=lambda row: row["sequence"])

    @staticmethod
    def _advance(ui, changes, *, expected, uow):
        """Check-and-set the mutable counters of one UI row inside the applying transaction."""
        assignments = ", ".join(f'"{name}" = ?' for name in changes)
        conditions = " AND ".join(f'"{name}" = ?' for name in expected)
        cursor = uow.connection.execute(
            f"UPDATE integrated_uis SET {assignments} WHERE business_id=? AND id=? AND {conditions}",
            [*changes.values(), ui["business_id"], ui["id"], *expected.values()])
        if cursor.rowcount != 1:
            raise DomainError("revision_conflict", "This UI changed; reload it",
                              current_revision=ui["data_revision"])

    # -- notification helpers -------------------------------------------------------------------------------------

    @staticmethod
    def _require_notification(ui):
        if not ui["task_id"]:
            raise DomainError("task_required", "Associate this UI with a Task before notifying anyone")

    def _require_notifier(self):
        if self.notifier is None:
            raise DomainError("operation_unavailable", "Notification delivery is not installed in this host")

    def _notification_key(self, ui, request, revision):
        explicit = request.get("notification_key")
        if explicit is not None:
            return limits.notification_key(explicit)
        return limits.notification_key(f'{ui["id"]}:{limits.event_type(request["type"])}:{revision}')

    @staticmethod
    def _payload_hash(request, revision):
        return canonical_hash({"type": limits.event_type(request["type"]), "data": plain(request.get("data")),
                               "revision": revision})

    def _notification(self, uow, ui, key):
        found = uow.connection.execute(
            "SELECT * FROM integrated_ui_events WHERE business_id=? AND ui_id=? AND notification_key=?",
            (ui["business_id"], ui["id"], key)).fetchone()
        return None if found is None else decode_row(found)

    def _duplicate(self, ctx, ui, request, revision, uow):
        """The saved receipt of an already-delivered semantic notification (never a second delivery)."""
        self._require_notifier()
        key = self._notification_key(ui, request, revision)
        existing = self._notification(uow, ui, key)
        if existing is None:
            raise DomainError("invalid_plan", "The saved notification is no longer available")
        if existing["notification_payload_hash"] != self._payload_hash(request, revision):
            raise DomainError("notification_key_conflict",
                              "This notification key was already used for different content",
                              details={"event_id": existing["id"], "notification_key": key})
        return self._stored_notification(existing)

    def _deliver(self, ctx, ui, event, uow):
        """Ask the installed notifier for the recipients, then save its receipt on the event in this transaction."""
        self._require_notifier()
        receipt = self.notifier(ctx, self.ui_dto(ui, notify_authorized=True), self.event_dto(event), uow)
        if not isinstance(receipt, Mapping):
            raise DomainError("invalid_result", "The installed notifier returned no delivery receipt")
        inbox_event_id, delivery_count = receipt.get("inbox_event_id"), receipt.get("delivery_count")
        if inbox_event_id is not None and type(inbox_event_id) is not int:
            raise DomainError("invalid_result", "The installed notifier returned an invalid inbox event")
        if type(delivery_count) is not int or delivery_count < 0:
            raise DomainError("invalid_result", "The installed notifier returned no delivery count")
        status = "queued" if delivery_count else "no_recipients"
        uow.connection.execute(
            "UPDATE integrated_ui_events SET notification_status=?,inbox_event_id=?,delivery_count=? "
            "WHERE business_id=? AND id=?",
            (status, inbox_event_id, delivery_count, ui["business_id"], event["id"]))
        return {"event_id": event["id"], "inbox_event_id": inbox_event_id,
                "delivery_count": delivery_count, "status": status}