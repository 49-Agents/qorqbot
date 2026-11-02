"""The operational inbox: the inbox service, event finalization, direct messages, broadcasts and related-work routing.

The host supplies the facts and effects this module does not own: managed workers, private owner channels, the email
integration, Focus cards, incidents and delegated assignments (``InboxHost``), the routing of the captures it alone understands
(``EventRouting``), who is directed to the owner, delegated worker bindings, the forwarding of an accepted message to its owner,
terminal delivery and input (``MessageHost``), and which agents are active globals (the ``Broadcasts``/``BroadcastDispatch``
arguments). The selected extensions' source handlers are an explicit argument too. None of them has a default, so a module
missing its host adapter fails when constructed rather than silently doing nothing. The schema, history and catalog declarations
are data for the host's assembly; the module does not create a contribution of its own."""

from . import related
from .broadcasts import Broadcasts
from .catalog import BROADCAST_READ_ENTRIES, MUTATION_ENTRIES, READ_ENTRIES
from .dispatch import BroadcastDispatch
from .events import ROUTING_METHODS, EventFinalizer, EventRouting
from .history import CONTRIBUTION as HISTORY_CONTRIBUTION
from .messages import HOST_METHODS, MessageHost, Messages
from .policy import INBOX_OPERATIONS, core_default
from .schema import (BROADCAST_SCHEMA, CAPTURE_SCHEMA, CHANGE_SCHEMA, CLASSIFICATIONS, MESSAGE_SCHEMA, PRINCIPAL_CAPTURE_SCHEMA,
                     RELATIONSHIP_CAPTURE_SCHEMA, TABLE_NAMES, TABLE_SPECS)
from .service import HOST_METHODS as INBOX_HOST_METHODS, THREAD_SOURCES, InboxHost, InboxService, visible_types

__all__ = ["BROADCAST_READ_ENTRIES", "BROADCAST_SCHEMA", "BroadcastDispatch", "Broadcasts", "CAPTURE_SCHEMA", "CHANGE_SCHEMA",
           "CLASSIFICATIONS", "EventFinalizer", "EventRouting", "HISTORY_CONTRIBUTION", "HOST_METHODS", "INBOX_HOST_METHODS",
           "INBOX_OPERATIONS", "InboxHost", "InboxService", "MESSAGE_SCHEMA", "MUTATION_ENTRIES", "MessageHost", "Messages",
           "PRINCIPAL_CAPTURE_SCHEMA", "READ_ENTRIES", "RELATIONSHIP_CAPTURE_SCHEMA", "ROUTING_METHODS", "TABLE_NAMES",
           "TABLE_SPECS", "THREAD_SOURCES", "core_default", "related", "visible_types"]
