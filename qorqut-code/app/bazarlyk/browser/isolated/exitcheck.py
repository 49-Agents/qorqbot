"""Observe the public exit IP through the in-namespace relay; never by navigating Chrome."""

import http.client
import ipaddress
import json
import ssl

from qorqut_operations.contracts import DomainError


def observed_exit_ip(relay_port, check, *, timeout=15, context=None):
    """HTTPS request to a fixed host through the relay; returns an IPv4 string or raises OSError-like failure."""
    host, path = check
    connection = http.client.HTTPSConnection("127.0.0.1", relay_port, timeout=timeout,
                                             context=context or ssl.create_default_context())
    connection.set_tunnel(host, 443)
    try:
        connection.request("GET", path, headers={"Accept": "application/json", "User-Agent": "qorqut-exit-check"})
        response = connection.getresponse()
        body = response.read(4096)
        if response.status != 200:
            raise OSError("exit check refused")
        return str(ipaddress.IPv4Address(json.loads(body)["ip"]))
    except (http.client.HTTPException, ValueError, KeyError, TypeError) as error:
        raise OSError("exit check failed") from error
    finally:
        connection.close()


def evaluate(relay_port, check, expected, **options):
    """('ok', ip) when it matches, ('mismatch', ip) when another IP answered, ('failed', None) otherwise."""
    try:
        observed = observed_exit_ip(relay_port, check, **options)
    except OSError:
        return "failed", None
    return ("ok" if observed == expected else "mismatch"), observed


def require_exit(relay_port, check, expected, **options):
    result, observed = evaluate(relay_port, check, expected, **options)
    if result == "mismatch":
        raise DomainError("invalid_configuration", "The proxy exits from a different IP than the assigned expected_exit_ip")
    if result == "failed":
        raise DomainError("operation_unavailable", "The assigned proxy did not answer the exit check")
    return observed
