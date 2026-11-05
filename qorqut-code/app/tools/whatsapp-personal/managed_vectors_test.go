package main

// Cross-language fingerprint vectors. The same inputs and digests are asserted by the host's Python tests
// (app/tests/test_whatsapp_signoffs_fingerprint.py): Go's json.Marshal and the host's reimplementation must agree.
type fingerprintVector struct{ chat, text, digest string }

func fingerprintVectors() []fingerprintVector {
	return []fingerprintVector{
		{"friend@s.whatsapp.net", "plain ascii", "c5b0949866d4a749eb561f68d37a0a889ea85c5fd4fad003cbb2c5abe41f0a99"},
		{"friend@s.whatsapp.net", "Привет, 世界 🌍 café", "59b20e604f5923ac3a4468c905110703463bfab7ebf791abe7cdb871367dbc6f"},
		{"friend@s.whatsapp.net", "a<b>c & d", "b11e10249d5e477dd09192429cdaa5002b261182077115a38d4486260934e8d6"},
		{"friend@s.whatsapp.net", "line1\nline2\r\n\ttab", "a0f1fd84b9df4f8c780c08f384944a494577eb16dce2187fbc670da8b6db1205"},
		{"friend@s.whatsapp.net", "sep and end", "eed296f1f8d00d356c3d2b2f265f5d228ccb54b7d6f8c4434bba499f2ccda321"},
		{"friend@s.whatsapp.net", "quote \" backslash \\ slash /", "0ecd8952425ddb8fd325ef85135e7e5b2722f65b1d38fd9f0de9fec547ef1a2b"},
		{"friend@s.whatsapp.net", "ctl \b\f\x01\x1f\x7f end", "dd947d124005c8fab9a29e592202717ec7438b3ec850badfd858b7af297ffe04"},
		{"friend@s.whatsapp.net", "é combining", "ab9b27cf1d1f492feacc33963f646e146892ce3ecacb02409f457f554554254a"},
		{"120363000000000000@g.us", "<&> \n", "d900287c801042807686a45ee625a3de1055a68aed4b7617b1bd8bf338be1598"},
		{"friend@s.whatsapp.net", "bound\u007fary ‧‪ \u0080 \U0001F600", "c2b185ccbcddf2dd637ec6e375dc4a5bb782ff0b2a98752aa696c20c2914551d"},
	}
}
