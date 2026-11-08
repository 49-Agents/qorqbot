"""The host's CRM composition: the aggregate ``crm_activity_list`` over the Domain's local activity and the composed
readers of directly linked sources. The CRM itself (Table kind, memberships, recorded observations) is the Operations
Domain's ``qorqut_operations.crm``; nothing here reimplements it."""

from .activity import CRMActivityOperations, NAME as ACTIVITY_LIST

__all__ = ["ACTIVITY_LIST", "CRMActivityOperations"]
