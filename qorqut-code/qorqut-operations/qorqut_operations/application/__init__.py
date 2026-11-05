"""Application contracts, operation registration and the one authenticated invocation path."""

from .catalog import CatalogComposition, CatalogContribution, compose_catalog
from .delegation import DelegatedCreationGroup
from .engine import Application, ApprovalBinding
from .registry import CatalogEntry, OperationDescriptor, OperationRegistry, Registry
from .review import BoundResource, ReviewScopeException

__all__ = ["Application", "ApprovalBinding", "BoundResource", "CatalogComposition", "CatalogContribution",
           "CatalogEntry", "DelegatedCreationGroup", "OperationDescriptor", "OperationRegistry", "Registry",
           "ReviewScopeException", "compose_catalog"]
