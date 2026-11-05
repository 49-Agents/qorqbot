"""Configuration rejection tests; network behavior is tested with real sockets."""

import json
from ipaddress import IPv4Address, IPv4Network

import pytest

from bazarlyk.sandbox.firewall import gateway_rules, host_bridge_rules
from bazarlyk.sandbox.network import NON_PUBLIC_V4, ProxyEndpoint, RoutingPolicy, gateway_config


@pytest.mark.parametrize("name", ["*", "*.github.com", "github.com/path", "github.com:443", "localhost", "1.1.1.1", "a..com", "a.com\n", "-a.com"])
def test_direct_routes_cannot_use_wildcards_ips_or_malformed_domains(name):
    with pytest.raises(ValueError):
        RoutingPolicy(direct_domains=(name,))


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "100.64.0.1", "198.18.0.1", "10.0.0.1"])
def test_private_proxy_address_cannot_open_gateway_exception(address):
    with pytest.raises(ValueError):
        ProxyEndpoint("http", address, 8080)
    with pytest.raises(ValueError):
        gateway_rules(agent_interface="eth1", browser_interface="eth2", proxy_address=address, proxy_port=8080)


@pytest.mark.parametrize("address", ["1.1.1.1", "127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "240.0.0.1"])
def test_agent_api_is_an_explicit_private_bridge_binding(address):
    with pytest.raises(ValueError):
        RoutingPolicy(agent_api_address=address, agent_api_port=1048)
    with pytest.raises(ValueError):
        gateway_rules(agent_interface="eth1", browser_interface="eth2", proxy_address="1.2.3.4", proxy_port=8080,
                      agent_api=(address, 1048))


@pytest.mark.parametrize("value", ["eth0; echo bad", "eth0\n", "", "a" * 16])
def test_interface_names_are_not_shell_configuration(value):
    with pytest.raises(ValueError):
        host_bridge_rules(value)


def test_credentials_are_private_and_https_proxy_verifies_original_name():
    endpoint = ProxyEndpoint.from_url("https://test-user:test-password@proxy.example:443", resolved_address="1.2.3.4")
    assert "test-user" not in repr(endpoint) and "test-password" not in repr(endpoint)
    value = gateway_config(RoutingPolicy(), endpoint)
    assert value["log"]["disabled"]
    upstream = value["outbounds"][0]
    assert upstream["server"] == "1.2.3.4"
    assert upstream["tls"] == {"enabled": True, "server_name": "proxy.example"}
    assert upstream["password"] == "test-password"
    assert "insecure" not in json.dumps(value)


def test_metadata_tailnet_and_special_use_ranges_are_blocked():
    ranges = tuple(IPv4Network(v) for v in NON_PUBLIC_V4)
    for value in ("0.1.2.3", "10.1.2.3", "100.64.12.45", "127.1.2.3", "169.254.169.254", "172.20.1.2",
                  "192.0.0.1", "192.0.2.1", "192.88.99.1", "192.168.1.2", "198.19.1.1", "198.51.100.1",
                  "203.0.113.1", "224.1.2.3", "240.1.2.3", "255.255.255.255"):
        assert any(IPv4Address(value) in network for network in ranges), value
