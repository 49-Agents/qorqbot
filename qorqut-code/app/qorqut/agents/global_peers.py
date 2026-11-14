"""Explicit global peer authority, independent of private account-mail authority."""


def global_peer(uow, identity):
    """True for the configured, active coordinator: the one global peer."""
    from qorqut.management.identity import configured_ordubashy
    return identity == configured_ordubashy(uow)
