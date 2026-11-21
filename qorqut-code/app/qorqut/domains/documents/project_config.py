"""Project settings: the host adapter behind ``project_config_get``, ``project_config_update`` and ``project_config_patch``.

The operations are Operations' (``qorqut_operations.projects.settings_operations``): the project row, the merge, the
revision check and the generic validation. A project's configuration row also holds host state (identity, owner and
workspace paths, engineering and owner-session sections) with host authority rules; ``HostSettings``
is the required ``SettingsHost`` that applies them at the points Operations calls. They use no document store."""

from __future__ import annotations

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.projects.settings_operations import ProjectConfigOperations as SettingsOperations

_BROWSER_UNSUPPORTED = "Project browser settings are not supported; agents use their own configured browsers"


def _retain_stored_browser(value, before):
    """Projects have no browser settings. A ``browser`` section an older installation stored is inert and kept exactly as
    it was (a replacement-style write never drops it); it can be neither set nor changed, and nothing reads it."""
    old = before.get("browser")
    if "browser" not in value or value["browser"] is None:
        if old is None:
            value.pop("browser", None)
        else:
            value["browser"] = old
    elif value["browser"] != old:
        raise DomainError("validation_error", _BROWSER_UNSUPPORTED)


class HostSettings:
    """``paths`` are the instance paths of this host."""

    def __init__(self, paths):
        self.paths = paths

    def require_patch_authority(self, ctx, uow):
        from qorqut.domains.project_settings import require_settings_authority
        require_settings_authority(ctx, uow)

    def prepare(self, ctx, value, before_config, args, operation, uow):
        from qorqut.owner_sessions import protect_owner_session_configuration
        # Run before the other owner-session preservers so an
        # explicit patch null/deletion is rejected rather than normalized
        # into an apparently unchanged protected section.
        deleted = ({key for key, item in plain(args["changes"]).items() if item is None}
                   if operation == "project_config_patch" and isinstance(args.get("changes"), dict) else ())
        protect_owner_session_configuration(value, before_config,
                                            owner_session=ctx.owner_session_project_id is not None,
                                            explicit_deletions=deleted)
        from qorqut.agents.engineering import has_ordubashy_settings_authority, prepare_authority
        delegated = has_ordubashy_settings_authority(ctx, uow)
        prepare_authority(value, before_config, principal_kind=ctx.principal_kind, delegated=delegated)
        from qorqut.owner_sessions import prepare_owner_sessions
        prepare_owner_sessions(value, before_config, principal_kind=ctx.principal_kind)
        _retain_stored_browser(value, before_config)

    def validate_targets(self, ctx, plan, uow):
        if plan.operation_name == "project_config_patch":
            from qorqut.domains.project_settings import require_settings_authority
            require_settings_authority(ctx, uow)
        if plan.operation_name in {"project_config_update", "project_config_patch"} and ctx.principal_kind != "human":
            from qorqut.agents.engineering import has_ordubashy_settings_authority
            before = uow.repository("projects").get(ctx.project_id, required=True)["config_json"]
            proposed = next(change.values["config_json"] for change in plan.changes if change.resource.resource_type == "projects")
            if any(key in proposed and proposed[key] != before.get(key, {} if key == "engineering" else None)
                   for key in ("engineering",)) and not has_ordubashy_settings_authority(ctx, uow):
                raise DomainError("forbidden", "Protected project settings require current owner or Ordubashy authority")
        if plan.operation_name in {"project_config_update", "project_config_patch"}:
            before = uow.repository("projects").get(ctx.project_id, required=True)["config_json"]
            proposed = plain(next(change.values["config_json"] for change in plan.changes
                                  if change.resource.resource_type == "projects"))
            from qorqut.owner_sessions import protect_owner_session_configuration
            protect_owner_session_configuration(proposed, before,
                                                owner_session=ctx.owner_session_project_id is not None)
            if proposed.get("browser") != before.get("browser"):
                raise DomainError("validation_error", _BROWSER_UNSUPPORTED)


class ProjectConfigOperations(SettingsOperations):
    """The project-settings operations bound to this host's ``SettingsHost``."""

    def __init__(self, paths, *, clock=utc_clock):
        super().__init__(host=HostSettings(paths), clock=clock)
