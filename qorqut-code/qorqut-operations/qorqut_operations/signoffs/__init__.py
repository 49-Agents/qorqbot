"""Exact owner decisions and resumable application, independent of any agent session.

``SignoffService`` is the extracted v1 Signoff machinery. Its host dependencies are the typed ``SignoffPorts``;
the prepared file executor and receipts it applies through are the ones in ``qorqut_operations.storage``."""

from .platform_approval import PlatformApproval
from .ports import (ABSENT, Absent, EmailDecisions, ExecutionPresentation, FocusAttention,
                    ManagementQueries, OwnerTaskAttention, PeerIdentities, PlatformActions, ReferenceTarget,
                    ReferenceVocabulary, SignoffPorts)
from .service import SIGNOFF_ENTRIES, SIGNOFF_REQUIRED_TABLES, SignoffService, storage_contribution

__all__ = ["ABSENT", "Absent", "EmailDecisions", "ExecutionPresentation", "FocusAttention",
           "ManagementQueries", "OwnerTaskAttention", "PeerIdentities", "PlatformActions", "PlatformApproval",
           "ReferenceTarget", "ReferenceVocabulary", "SIGNOFF_ENTRIES", "SIGNOFF_REQUIRED_TABLES",
           "SignoffPorts", "SignoffService", "storage_contribution"]
