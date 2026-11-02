"""Composition of the host's neutral items (see the package docstring)."""

from dataclasses import dataclass

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.relationships import ContextReviews, RelationshipOperations

from .adapters import relationship_ports
from .lookup import HostItemCandidates, HostItems
from .vocabulary import ITEM_TYPES
from .resources import RelatedResources


@dataclass(frozen=True)
class NeutralItems:
    related: RelatedResources
    items: ItemResolver
    relationships: RelationshipOperations
    reviews: ContextReviews


def build_neutral_items(application, *, documents, clock=utc_clock, extensions=()) -> NeutralItems:
    """``extensions`` are the selected extensions' typed item kinds (engine ``ItemContribution`` values)."""
    related = RelatedResources(application, documents)
    contribution = ItemContribution("host-items", ITEM_TYPES, HostItems(application, documents, related))
    items = ItemResolver(application, (contribution, *extensions), candidates=HostItemCandidates(extensions))
    ports = relationship_ports(items, documents=documents, clock=clock)
    relationships = RelationshipOperations(items, ports, clock=clock)
    return NeutralItems(related, items, relationships, relationships.reviews)
