"""Validation of the persistent installation configuration that a frozen plugin adoption selects."""
from __future__ import annotations

import json

from .errors import ReleaseError


def validate_config(config):
    from importlib.resources import files
    from jsonschema import Draft202012Validator
    schema=json.loads(files('qorqut.release_worker').joinpath('installation.schema.json').read_text())
    errors=sorted(Draft202012Validator(schema).iter_errors(config),key=lambda error:str(list(error.path)))
    if errors:
        raise ReleaseError('Invalid installation field: '+'.'.join(map(str,errors[0].path)))
    # The extension selection is structural here: each package validates its own configuration when the host starts it.
    from qorqut.extensions import validate_entries
    from qorqut_operations.contracts import DomainError
    try:
        validate_entries(config.get('extensions'))
    except DomainError as exc:
        raise ReleaseError('Invalid extensions installation selection') from exc
    from qorqut.bootstrap.client import service_url
    service_url(config['origin'])
    return config
