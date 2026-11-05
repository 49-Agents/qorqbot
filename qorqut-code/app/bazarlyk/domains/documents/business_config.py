"""Project settings: the host adapter behind ``business_config_get``, ``business_config_update`` and ``business_config_patch``.

The operations are Operations' (``qorqut_operations.projects.settings_operations``): the project row, the merge, the
revision check and the generic validation. A project's configuration row also holds host state (identity, owner and
workspace paths, browser, engineering and owner-session sections) with host authority rules; ``HostSettings``
is the required ``SettingsHost`` that applies them at the points Operations calls. They use no document store."""

from __future__ import annotations

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.projects.settings_operations import BusinessConfigOperations as SettingsOperations


class HostSettings:
    """``paths`` are the instance paths the browser-settings rules check against."""

    def __init__(self, paths):
        self.paths = paths

    def environment_status(self, uow, business_id):
        from bazarlyk.sandbox.state import environment_status
        return environment_status(uow, business_id)

    def require_patch_authority(self, ctx, uow):
        from bazarlyk.domains.business_settings import require_settings_authority
        require_settings_authority(ctx, uow)

    def prepare(self, ctx, value, before_config, args, operation, uow):
        from bazarlyk.owner_sessions import protect_owner_session_configuration
        # Run before the other owner-session preservers so an
        # explicit patch null/deletion is rejected rather than normalized
        # into an apparently unchanged protected section.
        deleted = ({key for key, item in plain(args["changes"]).items() if item is None}
                   if operation == "business_config_patch" and isinstance(args.get("changes"), dict) else ())
        protect_owner_session_configuration(value, before_config,
                                            owner_session=ctx.owner_session_business_id is not None,
                                            explicit_deletions=deleted)
        from bazarlyk.agents.engineering import has_ordubashy_settings_authority, prepare_authority
        delegated = has_ordubashy_settings_authority(ctx, uow)
        prepare_authority(value, before_config, principal_kind=ctx.principal_kind, delegated=delegated)
        from bazarlyk.owner_sessions import prepare_owner_sessions
        prepare_owner_sessions(value, before_config, principal_kind=ctx.principal_kind)
        from bazarlyk.browser.config import prepare_config
        prepare_config(value, before_config, paths=self.paths,
                       business_id=ctx.business_id, principal_kind=ctx.principal_kind, uow=uow, delegated=delegated)

    def validate_targets(self, ctx, plan, uow):
        if plan.operation_name == "business_config_patch":
            from bazarlyk.domains.business_settings import require_settings_authority
            require_settings_authority(ctx, uow)
        if plan.operation_name in {"business_config_update", "business_config_patch"} and ctx.principal_kind != "human":
            from bazarlyk.agents.engineering import has_ordubashy_settings_authority
            before = uow.repository("businesses").get(ctx.business_id, required=True)["config_json"]
            proposed = next(change.values["config_json"] for change in plan.changes if change.resource.resource_type == "businesses")
            if any(key in proposed and proposed[key] != before.get(key, {} if key == "engineering" else None)
                   for key in ("engineering", "browser")) and not has_ordubashy_settings_authority(ctx, uow):
                raise DomainError("forbidden", "Protected project settings require current owner or Ordubashy authority")
        if plan.operation_name in {"business_config_update", "business_config_patch"}:
            before = uow.repository("businesses").get(ctx.business_id, required=True)["config_json"]
            proposed = plain(next(change.values["config_json"] for change in plan.changes
                                  if change.resource.resource_type == "businesses"))
            from bazarlyk.owner_sessions import protect_owner_session_configuration
            protect_owner_session_configuration(proposed, before,
                                                owner_session=ctx.owner_session_business_id is not None)
            # Preparation may precede application, and prepared changes for other
            # businesses commit independently: re-check identity, IP uniqueness
            # and mode-change preconditions inside this writing transaction.
            from bazarlyk.browser.config import validate_applied
            validate_applied(proposed.get("browser"), before.get("browser"), paths=self.paths,
                             business_id=ctx.business_id, uow=uow)


class BusinessConfigOperations(SettingsOperations):
    """The project-settings operations bound to this host's ``SettingsHost``."""

    def __init__(self, paths, *, clock=utc_clock):
        super().__init__(host=HostSettings(paths), clock=clock)
