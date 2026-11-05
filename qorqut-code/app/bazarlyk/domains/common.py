"""Helpers of the retained domains that the Operations engine does not own.

Plan building, record application, paging, schemas and revision checks are ``qorqut_operations.application.plans``;
callers import those names from there."""

from __future__ import annotations

from qorqut_operations.contracts import plain


def config(uow, business_id, domain, default):
    row = uow.repository("business_configs", business_id=business_id).get(domain)
    return (plain(row["config_json"]) if row else plain(default)), row
