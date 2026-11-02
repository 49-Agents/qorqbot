"""The assembly of the neutral items: the one item contribution, resolver, relationship operations and context reviews.

The host builds its concrete lookup, candidates, related-resource resolver and relationship ports (the ports need the
resolver, hence ``ports(items)``) and hands them here once; the result is exposed independently by the host's domains and
handed to Tasks, documents, Focus and the inbox as consumers. Nothing here depends on a Task service."""

from __future__ import annotations

from dataclasses import dataclass

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.records.resources import RelatedResources
from qorqut_operations.records.vocabulary import ITEM_TYPES
from qorqut_operations.relationships import ContextReviews, RelationshipOperations


@dataclass(frozen=True)
class NeutralItems:
    related: RelatedResources
    items: ItemResolver
    relationships: RelationshipOperations
    reviews: ContextReviews


def compose_neutral_items(application, *, related, lookup, candidates, ports, extensions=(), clock=utc_clock) -> NeutralItems:
    """``extensions`` are the selected extensions' typed item kinds (engine ``ItemContribution`` values), registered after
    the one ``host-items`` contribution; ``ports(items)`` returns the ``RelationshipPorts`` over the built resolver."""
    contribution = ItemContribution("host-items", ITEM_TYPES, lookup)
    items = ItemResolver(application, (contribution, *extensions), candidates=candidates)
    relationships = RelationshipOperations(items, ports(items), clock=clock)
    return NeutralItems(related, items, relationships, relationships.reviews)
