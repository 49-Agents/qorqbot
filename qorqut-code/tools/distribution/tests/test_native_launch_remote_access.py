"""Remote access: the saved address other computers on a private network use for the dashboard.

    python3.12 -m unittest discover -s tools/distribution/tests -v
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_launch import REMOTE_ACCESS, remote_access, remote_origin, remote_origins  # noqa: E402

ADDRESS = 'https://desk.example.ts.net:1049'


class RemoteOriginTests(unittest.TestCase):
    def test_keeps_scheme_host_and_port_only(self):
        self.assertEqual(remote_origin(' HTTPS://Desk.Example.ts.net:1049/ '), ADDRESS)
        self.assertEqual(remote_origin('https://desk.example.ts.net'), 'https://desk.example.ts.net')
        self.assertEqual(remote_origin('http://[fd7a::1]:1049'), 'http://[fd7a::1]:1049')

    def test_refuses_anything_that_is_not_a_plain_address(self):
        for value in ('desk.example.ts.net', 'ftp://desk.example.ts.net', 'https://', 'https://me:pw@desk.example.ts.net',
                      'https://desk.example.ts.net/#/global/agents', 'https://desk.example.ts.net/x', 'https://desk.example.ts.net?a=1',
                      'https://desk.example.ts.net:notaport'):
            with self.subTest(value), self.assertRaises(ValueError):
                remote_origin(value)


class RemoteAccessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_off_until_set(self):
        self.assertEqual(remote_origins(self.root), [])
        self.assertEqual(remote_access(self.root), 'Remote access is off.')

    def test_set_show_and_clear(self):
        message = remote_access(self.root, ADDRESS + '/', port=1049)
        self.assertIn(ADDRESS, message)
        self.assertIn('tailscale serve --bg --https=1049 http://127.0.0.1:1049', message)
        self.assertNotIn('Stop and start', message)
        self.assertEqual(remote_origins(self.root), [ADDRESS])
        self.assertEqual(remote_access(self.root), 'Remote access: ' + ADDRESS)
        self.assertIn('Stop and start', remote_access(self.root, 'off', running=True))
        self.assertEqual(remote_origins(self.root), [])
        self.assertFalse((self.root / REMOTE_ACCESS).exists())

    def test_default_https_port_and_restart_note(self):
        message = remote_access(self.root, 'https://desk.example.ts.net', running=True, port=1049)
        self.assertIn('--https=443 http://127.0.0.1:1049', message)
        self.assertIn('Stop and start', message)

    def test_bad_address_changes_nothing(self):
        remote_access(self.root, ADDRESS)
        with self.assertRaises(ValueError):
            remote_access(self.root, 'desk.example.ts.net')
        self.assertEqual(remote_origins(self.root), [ADDRESS])

    def test_damaged_record_counts_as_off(self):
        path = self.root / REMOTE_ACCESS
        path.parent.mkdir(parents=True)
        for text in ('not json', '{"origins": "x"}', '{"origins": ["https://a.example/path"]}', '[]'):
            path.write_text(text)
            self.assertEqual(remote_origins(self.root), [], text)


if __name__ == '__main__':
    unittest.main()
