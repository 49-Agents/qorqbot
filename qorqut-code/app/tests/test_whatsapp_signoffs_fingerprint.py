"""The host's Go-compatible request fingerprint, locked against vectors computed by the connector's own Go code."""

import hashlib

import pytest

from bazarlyk.service.whatsapp_send import go_json_string, payload_fingerprint
from qorqut_operations.contracts import DomainError

# The same inputs and digests as ``fingerprintVectors`` in tools/whatsapp-personal/managed_vectors_test.go, where Go's
# json.Marshal computed them (HTML escaping on). Either encoder drifting breaks one of the two test suites.
VECTORS = [
    ("friend@s.whatsapp.net", "plain ascii", "c5b0949866d4a749eb561f68d37a0a889ea85c5fd4fad003cbb2c5abe41f0a99"),
    ("friend@s.whatsapp.net", "Привет, 世界 🌍 café", "59b20e604f5923ac3a4468c905110703463bfab7ebf791abe7cdb871367dbc6f"),
    ("friend@s.whatsapp.net", "a<b>c & d", "b11e10249d5e477dd09192429cdaa5002b261182077115a38d4486260934e8d6"),
    ("friend@s.whatsapp.net", "line1\nline2\r\n\ttab", "a0f1fd84b9df4f8c780c08f384944a494577eb16dce2187fbc670da8b6db1205"),
    ("friend@s.whatsapp.net", "sep and end", "eed296f1f8d00d356c3d2b2f265f5d228ccb54b7d6f8c4434bba499f2ccda321"),
    ("friend@s.whatsapp.net", 'quote " backslash \\ slash /', "0ecd8952425ddb8fd325ef85135e7e5b2722f65b1d38fd9f0de9fec547ef1a2b"),
    ("friend@s.whatsapp.net", "ctl \b\f\x01\x1f\x7f end", "dd947d124005c8fab9a29e592202717ec7438b3ec850badfd858b7af297ffe04"),
    ("friend@s.whatsapp.net", "é combining", "ab9b27cf1d1f492feacc33963f646e146892ce3ecacb02409f457f554554254a"),
    ("120363000000000000@g.us", "<&> \n", "d900287c801042807686a45ee625a3de1055a68aed4b7617b1bd8bf338be1598"),
    ("friend@s.whatsapp.net", "bound\u007fary ‧‪ \u0080 \U0001F600", "c2b185ccbcddf2dd637ec6e375dc4a5bb782ff0b2a98752aa696c20c2914551d"),
]


@pytest.mark.parametrize("chat,text,digest", VECTORS)
def test_the_fingerprint_matches_the_connectors_go_encoding(chat, text, digest):
    assert payload_fingerprint(chat, text) == digest


def test_html_and_separator_characters_are_escaped_like_go():
    assert go_json_string("<>&  ") == '"\\u003c\\u003e\\u0026\\u2028\\u2029"'
    assert go_json_string("\n\r\t\"\\") == '"\\n\\r\\t\\"\\\\"'
    assert go_json_string("\x01") == '"\\u0001"'
    assert go_json_string("é世🌍") == '"é世🌍"'


def test_the_fingerprint_is_over_the_two_element_array_with_no_spaces():
    expected = hashlib.sha256(b'["friend@s.whatsapp.net","hello"]').hexdigest()
    assert payload_fingerprint("friend@s.whatsapp.net", "hello") == expected


def test_distinct_payloads_never_share_a_fingerprint():
    assert len({payload_fingerprint(c, t) for c, t, _ in VECTORS}) == len(VECTORS)
    assert payload_fingerprint("a@s.whatsapp.net", "b") != payload_fingerprint("a@s.whatsapp.net", "b ")
    assert payload_fingerprint("a@s.whatsapp.net", "b\n") != payload_fingerprint("a@s.whatsapp.net", "b")


def test_text_that_go_cannot_represent_is_refused():
    with pytest.raises(DomainError):
        go_json_string("lone surrogate \ud800")
