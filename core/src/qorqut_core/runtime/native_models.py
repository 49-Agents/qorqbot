"""Frozen D40 observation contracts; neither view grants input readiness."""

from typing import Literal

from ..contracts import Counter, Model, Timestamp


class NativeSetupView(Model):
    state: Literal["disabled", "waiting", "ready", "blocked", "unknown"] = "disabled"
    observed_at: Timestamp | None = None
    reason: Literal["not_configured", "unsupported_target", "waiting_terminal",
                    "configuration_installed", "setup_unavailable", "recovery_review",
                    "observation_stale"] = "not_configured"


class NativeCollectionView(Model):
    state: Literal["disabled", "waiting", "collecting", "blocked", "gap", "unknown"] = "disabled"
    observed_at: Timestamp | None = None
    reason: Literal["not_configured", "unsupported_target", "waiting_session", "waiting_source",
                    "active", "collection_blocked", "capture_gap", "parser_gap",
                    "recovery_review", "observation_stale"] = "not_configured"
    source_count: Counter = 0
    pending_archive_bytes: Counter = 0
    pending_parse_bytes: Counter = 0
