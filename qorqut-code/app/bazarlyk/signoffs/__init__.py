"""The host's side of the Operations Signoff machinery.

The Signoff service, its input requests, notes, rejection and notifications, and the exact-decision base of the
platform approvals are ``qorqut_operations.signoffs``. This package holds only what the product contributes to them:
the concrete ``SignoffPorts`` (``ports``) and the concrete email approval (``platform_review``)."""
