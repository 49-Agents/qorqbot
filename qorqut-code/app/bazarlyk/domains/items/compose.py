"""Composition of the host's neutral items: the host's lookup, resource and relationship adapters handed to the Operations
assembly (``qorqut_operations.records.compose``), which builds the one item contribution, resolver and relationship
operations."""

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.records.compose import NeutralItems, compose_neutral_items
from qorqut_operations.records.lookup import RecordCandidates, RecordItems
from qorqut_operations.records.vocabulary import RESOURCE_TYPES

from .adapters import relationship_ports
from .lookup import HostLookup
from .resources import build_related


def build_neutral_items(application, *, documents, clock=utc_clock, extensions=()) -> NeutralItems:
    """``extensions`` are the selected extensions' typed item kinds (engine ``ItemContribution`` values)."""
    related = build_related(application, documents)
    host = HostLookup()
    return compose_neutral_items(
        application, related=related, lookup=RecordItems(application, documents, related, RESOURCE_TYPES, host),
        candidates=RecordCandidates(RESOURCE_TYPES, host, extensions),
        ports=lambda items: relationship_ports(items, documents=documents, clock=clock), extensions=extensions, clock=clock)
