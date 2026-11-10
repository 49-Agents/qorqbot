"""Local CDP polling does no TLS/proxy work and accepts only a bounded HTTP response."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock, patch

from qorqut.browser.agent_browsers import cdp_healthy


class HealthProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.server.paths.append(self.path)
                self.send_response(self.server.status)
                self.send_header('Content-Length', str(len(self.server.body)))
                if self.server.status == 302:
                    self.send_header('Location', '/redirected')
                self.end_headers()
                self.wfile.write(self.server.body)

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.server.paths = []
        self.server.status = 200
        self.server.body = json.dumps({'webSocketDebuggerUrl': 'ws://127.0.0.1/devtools/browser/test'}).encode()

    def test_real_loopback_response_without_loading_tls_or_proxy_settings(self):
        with patch('ssl.create_default_context', side_effect=AssertionError('TLS context on local HTTP')), \
                patch.dict('os.environ', {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1'}):
            self.assertTrue(cdp_healthy(self.endpoint))
            self.assertTrue(cdp_healthy(self.endpoint + '/'))
        self.assertEqual(self.server.paths, ['/json/version', '/json/version'])

    def test_unhealthy_malformed_oversized_and_redirect_responses_fail_closed(self):
        for status, body in [(500, self.server.body), (302, self.server.body), (200, b'not json'),
                             (200, b'[]'), (200, b'{}'), (200, b'{"webSocketDebuggerUrl":null}'),
                             (200, b' ' * 65537 + self.server.body)]:
            with self.subTest(status=status, size=len(body)):
                self.server.status, self.server.body = status, body
                self.assertFalse(cdp_healthy(self.endpoint))
        self.assertNotIn('/redirected', self.server.paths)

    def test_nonlocal_or_ambiguous_targets_never_open_connections(self):
        with patch('qorqut.browser.agent_browsers.HTTPConnection') as connection:
            for endpoint in ('https://localhost:9222', 'http://example.com:9222', 'http://localhost',
                             'http://user:password@localhost:9222', self.endpoint + '/path',
                             self.endpoint + '?query', self.endpoint + '#fragment', 'http://localhost:bad'):
                self.assertFalse(cdp_healthy(endpoint), endpoint)
            connection.assert_not_called()

    def test_timeout_closes_connection_and_preserves_configured_deadline(self):
        connection = Mock()
        connection.getresponse.side_effect = TimeoutError('probe deadline')
        with patch('qorqut.browser.agent_browsers.HTTPConnection', return_value=connection) as create:
            self.assertFalse(cdp_healthy('http://[::1]:9222', timeout=.2))
        create.assert_called_once_with('::1', 9222, timeout=.2)
        connection.close.assert_called_once()

