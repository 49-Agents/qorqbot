"""Actions: immutable-revision completed work, handed to the composition's required on_revision port in the writing transaction."""

from .models import (ACTION_TEXT, AGENT_WORD_LIMIT, EVIDENCE, EVIDENCE_KEYS, RELATED_OBJECTS, action_key, concise, one_line,
                     origin_for, source_identity, word_count)
from .operations import ACTION_ENTRIES, ActionOperations
from .ports import RevisionAdmission, require_on_revision
from .schema import CATEGORIES, ORIGINS, storage_contribution
from .scope import action_namespace, change_namespace, classify, evidence_visible, is_owner, links, may_change, reader_scope, visible
from .writer import SOURCE_TYPE, change_action, labels_of, new_action, settle

__all__ = ["ACTION_ENTRIES", "ACTION_TEXT", "AGENT_WORD_LIMIT", "ActionOperations", "CATEGORIES", "EVIDENCE", "EVIDENCE_KEYS",
           "ORIGINS", "RELATED_OBJECTS", "RevisionAdmission", "SOURCE_TYPE", "action_key", "action_namespace", "change_action", "change_namespace", "classify", "concise",
           "evidence_visible", "is_owner", "labels_of", "links", "may_change", "new_action", "one_line", "origin_for",
           "reader_scope", "require_on_revision", "settle", "source_identity", "storage_contribution", "visible", "word_count"]
