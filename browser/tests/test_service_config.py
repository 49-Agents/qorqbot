import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
from pydantic import ValidationError

from qorqut_browser.config import Credentials, HandoffConfig, load_config, load_credentials, load_token
from qorqut_browser.contracts import BrowserError
from qorqut_browser.server import create_app
from wire_fixture import APPLICATION_TOKEN, TOKEN_A, BrowserServer, private_file


class ServiceConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser-config-r2-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.server = BrowserServer(self.root)
        self.addCleanup(self.server.close)

    def test_private_inputs_reject_bad_credentials_permissions_and_overrides(self):
        server = self.server
        raw = json.loads(server.credentials.read_text())
        for mutation in (lambda v: v.pop("application_token"),
                         lambda v: v["actors"][0].update(token="short"),
                         lambda v: v["actors"][0].update(token=APPLICATION_TOKEN),
                         lambda v: v["actors"][1].update(token=TOKEN_A),
                         lambda v: v["actors"][0].update(actor_id="bad-id"),
                         lambda v: v["actors"][0].pop("agent_id")):
            value = json.loads(json.dumps(raw))
            mutation(value)
            private_file(server.credentials, value)
            with self.assertRaises(BrowserError) as error:
                load_credentials(server.credentials)
            self.assertNotIn(TOKEN_A, str(error.exception))
            with self.assertRaises(BrowserError):
                create_app(server.config)
        private_file(server.credentials, raw)
        self.assertNotIn(TOKEN_A, repr(load_credentials(server.credentials)))
        with self.assertRaises(ValidationError) as error:
            Credentials.model_validate({**raw, "application_token": TOKEN_A})
        self.assertNotIn(TOKEN_A, str(error.exception))
        server.credentials.chmod(0o640)
        with self.assertRaises(BrowserError):
            load_credentials(server.credentials)
        server.credentials.chmod(0o600)
        link = self.root / "symlink"
        link.symlink_to(server.credentials)
        with self.assertRaises(BrowserError):
            load_credentials(link)
        token = private_file(self.root / "bridge.token", TOKEN_A)
        token.chmod(0o644)
        with self.assertRaises(BrowserError):
            load_token(token)
        config = server.config.model_dump(mode="json")
        path = private_file(self.root / "service.json", config)
        self.assertEqual(load_config(path), server.config)
        for key, value in (("fake_mode", True), ("workers", 2), ("host", "0.0.0.0")):
            private_file(path, {**config, key: value})
            with self.assertRaises(BrowserError):
                load_config(path)
        private_file(path, config)
        path.chmod(0o644)
        with self.assertRaises(BrowserError):
            load_config(path)
        for url in ("ftp://127.0.0.1/path", "http://user:password@127.0.0.1/path", "http://127.0.0.1/#fragment",
                    "/relative", "http://127.0.0.1:invalid/", "http://127.0.0.1/\n"):
            with self.assertRaises(ValidationError):
                HandoffConfig(url=url, token_file=token)
        # Manager amendment 1: deployment owns private destination routing.
        # Accept configured DNS syntactically without resolving/contacting it.
        endpoint = "https://core.internal:8443/handoff"
        self.assertEqual(HandoffConfig(url=endpoint, token_file=token).url, endpoint)

    def test_production_entrypoint_has_real_readiness_and_does_not_initialize_missing_state(self):
        server = self.server
        # The fixture never starts; release its reservation for the real entrypoint.
        server.listener.close()
        config_path = private_file(self.root / "service.json", server.config.model_dump(mode="json"))
        command = [sys.executable, "-m", "qorqut_browser.server", "--config", str(config_path)]
        log_path = private_file(self.root / "entrypoint.log", "")
        with log_path.open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, env={**os.environ, "PYTHONPATH": "src"})
            try:
                with httpx.Client(trust_env=False, timeout=.3) as client:
                    for _ in range(100):
                        self.assertIsNone(process.poll(), log_path.read_text())
                        try:
                            response = client.get(server.url + "/v1/identity", headers=server.headers())
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(.02)
                    else:
                        self.fail("Production entrypoint failed to listen")
                    self.assertFalse(response.json()["control"]["ready"])
                    expected = response.json()["control"]["binding"]
                    desired = {**expected, "control_epoch": expected["control_epoch"] + 1}
                    request = {"command_id": server.command()["command_id"], "expected": expected, "desired": desired,
                               "mode": "automation", "evidence": "Missing real desktop must remain closed"}
                    response = client.post(server.url + "/v1/browser/control", json=request,
                                           headers=server.headers(APPLICATION_TOKEN, server.actor_a), timeout=10)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["outcome"], "pending")
                    self.assertEqual(response.json()["error"], "not_ready")
                    self.assertFalse(server.store.status().ready)
                    self.assertEqual(list(server.config.browser.artifacts.iterdir()),
                                     [server.config.browser.artifacts / ".qorqut-browser-owner"])
            finally:
                process.terminate()
                process.wait(10)
        server.config.browser.database.unlink()
        missing = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(server.config.browser.database.exists())
        self.assertNotIn(TOKEN_A, missing.stderr + log_path.read_text())
        self.assertFalse(server.config.browser.state_file.exists())


if __name__ == "__main__":
    unittest.main()
