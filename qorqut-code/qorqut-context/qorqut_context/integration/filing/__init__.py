"""Context source ownership, accepted source versions, filings and their completion receipts.

The queue binds each source to one operational scope (``sources``); a filing adapter admits immutable source versions with
their registered job and queue input (``admission``) and receives one stable terminal receipt per filing from the queue's
required terminal-outcome contribution (``FilingOutcomes``); ``completion`` authorizes the receipt's one application and
reads the filing state both sides expose."""

from qorqut_context.integration.filing import admission, completion, envelope, outcomes, sources
from qorqut_context.integration.filing.admission import Admission, admit, new_filing_id
from qorqut_context.integration.filing.outcomes import FilingOutcomes
from qorqut_context.integration.filing.schema import FILING_KINDS, PROGRAMS, SOURCE_KINDS, storage_contribution
from qorqut_context.integration.filing.sources import SCOPE_CONFLICT, UNBOUND, binding, identity, reserve_source

__all__ = ["Admission", "FILING_KINDS", "FilingOutcomes", "PROGRAMS", "SCOPE_CONFLICT", "SOURCE_KINDS", "UNBOUND", "admission", "admit",
           "binding", "completion", "envelope", "identity", "new_filing_id", "outcomes", "reserve_source", "sources",
           "storage_contribution"]
