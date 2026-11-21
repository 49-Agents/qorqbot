"""The OpenRouter extension: how the owner's key is obtained and kept, who may read its status and what voice input sends."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from qorqut_operations.contracts import DomainError
from qorqut.extensions import ExtensionSet

# The extension is bundled source beside the host (qorqut-code/extensions/openrouter); it is not installed in the test environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "extensions" / "openrouter"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "extensions" / "treg"))

from qorqut_openrouter import config as settings, operations
from qorqut_openrouter.client import Answer, OpenRouterClient, multipart
from qorqut_openrouter.extension import SIGN_IN_SECONDS, can_return_to, challenge, create

# Made-up values in the shape of a key. They are assembled from words so a secret scanner does not read them as real ones.
KEY = "-".join(("made", "up", "openrouter", "key", "for", "tests"))
OTHER_KEY = "-".join(("another", "made", "up", "key", "for", "tests"))
CODE = "-".join(("made", "up", "sign", "in", "code"))
LOCAL = "http://127.0.0.1:8090"


class FakeClient:
    def __init__(self):
        self.exchanges, self.checked, self.recordings = [], [], []
        self.refuse_key = self.refuse_code = False
        self.issued = KEY

    def exchange(self, code, verifier):
        self.exchanges.append((code, verifier))
        if self.refuse_code:
            raise DomainError("openrouter_key_refused", "OpenRouter did not accept the sign-in")
        return self.issued

    def key_info(self, key):
        self.checked.append(key)
        if self.refuse_key:
            raise DomainError("openrouter_key_refused", "OpenRouter did not accept this key")
        return {"limit_usd": 10, "remaining_usd": 7.5, "used_usd": 2.5, "free_tier": False}

    def transcribe(self, key, audio, *, media_type, model, language=None, provider=None):
        self.recordings.append(NS(key=key, audio=audio, media_type=media_type, model=model, language=language,
                                  provider=provider))
        return {"text": "hello there", "cost_usd": 0.0004, "seconds": 3.2}


class Registry:
    def __init__(self):
        self.descriptors = {}

    def register(self, descriptor):
        self.descriptors[descriptor.name] = descriptor


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve() / "instance"
        (self.root / "config").mkdir(parents=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.client, self.now = FakeClient(), [1000.0]
        self.extension = create(None, client=self.client, clock=lambda: self.now[0])
        self.registry = Registry()
        self.profile = NS(data={"role": {"id": "ordubashy"}, "status": "active"}, scope=NS(kind="ecosystem"))
        resolver = NS(profile=lambda principal, connection=None: (self.profile, None))
        self.extension.install(NS(paths=NS(instance_root=self.root), registry=self.registry, resolver=resolver))
        self.agent = NS(principal_kind="agent", principal_id="agent-1", principal=object(), project_id=None)

    def status(self, *, coordinator="agent-1", context=None):
        with mock.patch("qorqut.management.ManagementService._coordinator", return_value=coordinator):
            return self.registry.descriptors[operations.STATUS].read(context or self.agent, {}, None, NS(connection=None))

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def saved(self):
        path = settings.key_path(self.root)
        return path.read_text().strip() if path.exists() else None


class DeclarationTest(Base):
    def test_the_selection_accepts_the_extension_its_one_read_and_its_three_owner_routes(self):
        ExtensionSet((create(),))
        descriptor = self.registry.descriptors[operations.STATUS]
        self.assertEqual((descriptor.effect_kind, descriptor.default_mode, descriptor.supports_signoff), ("read", "allow", False))
        self.assertEqual(set(descriptor.scope_kinds), {"global"})
        self.assertEqual({(route.method, route.path[-1]) for route in self.extension.owner_routes()},
                         {("POST", "key"), ("POST", "connect"), ("POST", "transcribe")})
        for route in self.extension.owner_routes():
            self.assertEqual(route.path[:3], ("root", "extensions", "openrouter"))

    def test_the_hosts_own_policy_offers_the_status_to_ordubashy_only(self):
        from qorqut.composition.catalog import builtin_registry
        from qorqut.composition.policy import resolve_operation_policy
        extension = create(None, client=FakeClient())
        registry = builtin_registry(ExtensionSet((extension,)))
        extension.install(NS(paths=NS(instance_root=self.root), registry=registry, resolver=None))
        for role, mode in (("ordubashy", "allow"), ("global-worker", "deny"), ("learning-applier", "deny")):
            self.assertEqual(resolve_operation_policy(operations.STATUS, {}, {}, registry, role_id=role).mode, mode, role)

    def test_it_is_selected_together_with_treg(self):
        # Both ship in every installation, so nothing they declare may clash.
        from qorqut import extensions
        chosen = extensions.select([{"factory": "qorqut_treg.extension:create", "config": {}},
                                    {"factory": "qorqut_openrouter.extension:create", "config": {}}])
        self.assertEqual(sorted(item.id for item in chosen.extensions), ["openrouter", "treg"])

    def test_configuration_allows_only_a_model_id_and_a_provider(self):
        self.assertEqual(settings.from_config(None).transcription_provider, "deepinfra")
        self.assertEqual(settings.from_config({"transcription_provider": ""}).transcription_provider, "")
        self.assertEqual(settings.from_config({"transcription_provider": "groq"}).transcription_provider, "groq")
        for wrong in ({"transcription_provider": "Deep Infra"}, {"transcription_provider": None}):
            self.refused("invalid_installation", settings.from_config, wrong)
        self.assertEqual(settings.from_config(None).transcription_model, "openai/whisper-large-v3-turbo")
        self.assertEqual(settings.from_config({"transcription_model": "openai/gpt-4o-mini-transcribe"}).transcription_model,
                         "openai/gpt-4o-mini-transcribe")
        for wrong in ({"transcription_model": "whisper"}, {"transcription_model": 3}, {"key": KEY}, []):
            self.refused("invalid_installation", settings.from_config, wrong)


class StatusTest(Base):
    def test_before_a_key_ordubashy_learns_how_the_owner_connects_and_nothing_is_contacted(self):
        answer = self.status()
        self.assertEqual(answer["status"], "not_set_up")
        self.assertIn("Settings, Connections", answer["how_to_connect"])
        self.assertIn("Never ask for the key in Chat", answer["how_to_connect"])
        self.assertEqual(self.client.checked, [])

    def test_once_connected_it_reports_the_keys_limit_and_never_the_key(self):
        settings.save_key(self.root, KEY)
        answer = self.status()
        self.assertEqual((answer["status"], answer["key"]["remaining_usd"]), ("connected", 7.5))
        self.assertNotIn(KEY, repr(answer))

    def test_a_key_openrouter_no_longer_accepts_is_reported_as_refused(self):
        settings.save_key(self.root, KEY)
        self.client.refuse_key = True
        self.assertEqual(self.status()["status"], "key_refused")

    def test_only_the_configured_ordubashy_in_the_global_scope_may_read_it(self):
        self.refused("forbidden", self.status, coordinator="another-agent")
        self.refused("forbidden", self.status, context=NS(principal_kind="human", principal_id="owner", principal=object(),
                                                         project_id=None))
        self.refused("forbidden", self.status, context=NS(principal_kind="agent", principal_id="agent-1", principal=object(),
                                                         project_id="b1"))
        self.profile.data["role"]["id"] = "global-worker"
        self.refused("forbidden", self.status)


class SignInTest(Base):
    def test_signing_in_sends_only_a_challenge_and_returns_to_the_dashboard_that_asked(self):
        url = urlsplit(self.extension.connect(LOCAL, {"action": "start"})["url"])
        query = parse_qs(url.query)
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "openrouter.ai", "/auth"))
        self.assertEqual(query["callback_url"], [LOCAL + "/"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(self.extension.connect(LOCAL, {"action": "finish", "code": CODE}), {"status": "connected"})
        (code, verifier), = self.client.exchanges
        self.assertEqual((code, query["code_challenge"]), (CODE, [challenge(verifier)]))
        self.assertNotIn(verifier, url.query)
        self.assertEqual((self.saved(), self.client.checked), (KEY, [KEY]))
        self.assertEqual(settings.key_path(self.root).stat().st_mode & 0o777, 0o600)

    def test_a_code_is_used_once_and_only_by_the_sign_in_that_asked_for_it(self):
        self.refused("openrouter_key_refused", self.extension.connect, LOCAL, {"action": "finish", "code": CODE})
        self.extension.connect(LOCAL, {"action": "start"})
        self.refused("openrouter_key_refused", self.extension.connect, "https://elsewhere.example", {"action": "finish", "code": CODE})
        self.extension.connect(LOCAL, {"action": "start"})
        self.extension.connect(LOCAL, {"action": "finish", "code": CODE})
        self.refused("openrouter_key_refused", self.extension.connect, LOCAL, {"action": "finish", "code": CODE})
        self.assertEqual(len(self.client.exchanges), 1)

    def test_a_sign_in_left_too_long_or_refused_by_openrouter_saves_nothing(self):
        self.extension.connect(LOCAL, {"action": "start"})
        self.now[0] += SIGN_IN_SECONDS + 1
        self.refused("openrouter_key_refused", self.extension.connect, LOCAL, {"action": "finish", "code": CODE})
        self.extension.connect(LOCAL, {"action": "start"})
        self.client.refuse_code = True
        self.refused("openrouter_key_refused", self.extension.connect, LOCAL, {"action": "finish", "code": CODE})
        self.extension.connect(LOCAL, {"action": "start"})
        self.client.refuse_code, self.client.refuse_key = False, True
        self.refused("openrouter_key_refused", self.extension.connect, LOCAL, {"action": "finish", "code": CODE})
        self.assertIsNone(self.saved())

    def test_signing_in_needs_a_dashboard_openrouter_can_return_to(self):
        for origin, allowed in (("http://localhost:8090", True), ("http://127.0.0.1", True), ("http://[::1]:8090", True),
                                ("https://qorqbot.example", True), ("http://192.168.1.20:8090", False),
                                ("http://qorqbot.example", False)):
            self.assertEqual(can_return_to(origin), allowed, origin)
        self.refused("operation_unavailable", self.extension.connect, "http://192.168.1.20:8090", {"action": "start"})

    def test_anything_but_start_or_finish_is_refused(self):
        for wrong in ({}, {"action": "start", "callback_url": "https://elsewhere.example"}, {"action": "finish"},
                      {"action": "finish", "code": 7}, {"action": "finish", "code": "short"}, [], None):
            self.refused("validation_error", self.extension.connect, LOCAL, wrong)


class KeyTest(Base):
    def test_a_pasted_key_is_checked_with_openrouter_then_kept_private_and_never_returned(self):
        self.assertEqual(self.extension.status(), "not_set_up")
        answer = self.extension.change_key({"key": f"  {KEY}\n"})
        self.assertEqual((answer, self.saved(), self.client.checked), ({"status": "connected"}, KEY, [KEY]))
        self.extension.change_key({"key": OTHER_KEY})
        self.assertEqual(self.saved(), OTHER_KEY)
        self.assertEqual(self.extension.change_key({"key": None}), {"status": "not_set_up"})
        self.assertIsNone(self.saved())

    def test_a_key_openrouter_refuses_or_a_malformed_one_is_not_saved(self):
        self.client.refuse_key = True
        self.refused("openrouter_key_refused", self.extension.change_key, {"key": KEY})
        for wrong in ({"key": "short"}, {"key": "has a space in the middle of it"}, {"key": 5}, {"token": KEY}, None):
            self.refused("validation_error", self.extension.change_key, wrong)
        self.assertIsNone(self.saved())

    def test_a_key_file_others_can_read_is_refused(self):
        settings.save_key(self.root, KEY)
        os.chmod(settings.key_path(self.root), 0o644)
        with self.assertRaises(Exception):
            settings.read_key(self.root)


class VoiceInputTest(Base):
    def test_a_recording_goes_to_openrouter_with_the_owners_key_and_the_configured_model(self):
        settings.save_key(self.root, KEY)
        answer = self.extension.transcribe(b"audio-bytes", "audio/webm", "en")
        self.assertEqual(answer, {"text": "hello there", "cost_usd": 0.0004})
        sent, = self.client.recordings
        self.assertEqual((sent.key, sent.audio, sent.media_type, sent.model, sent.language, sent.provider),
                         (KEY, b"audio-bytes", "audio/webm", "openai/whisper-large-v3-turbo", "en", "deepinfra"))

    def test_without_a_key_nothing_is_sent_and_the_owner_is_told_where_to_connect(self):
        problem = self.refused("operation_unavailable", self.extension.transcribe, b"audio-bytes", "audio/webm")
        self.assertIn("Settings, Connections", problem.message)
        self.assertEqual(self.client.recordings, [])

    def test_an_empty_or_unsupported_recording_or_a_wrong_language_is_refused_before_openrouter(self):
        settings.save_key(self.root, KEY)
        self.refused("validation_error", self.extension.transcribe, b"", "audio/webm")
        self.refused("validation_error", self.extension.transcribe, b"audio-bytes", "video/mp4")
        self.refused("validation_error", self.extension.transcribe, b"audio-bytes", "audio/webm", "english")
        self.assertEqual(self.client.recordings, [])


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.sent, self.answers = [], []
        self.client = OpenRouterClient(send=self.send)

    def send(self, method, path, **options):
        self.sent.append(NS(method=method, path=path, **options))
        return self.answers.pop(0)

    def test_a_code_is_traded_for_a_key_without_any_key_in_the_request(self):
        self.answers.append(Answer(200, {"key": KEY}))
        self.assertEqual(self.client.exchange(CODE, "verifier"), KEY)
        request, = self.sent
        self.assertEqual((request.method, request.path), ("POST", "/api/v1/auth/keys"))
        self.assertEqual(request.body, {"code": CODE, "code_verifier": "verifier", "code_challenge_method": "S256"})
        self.assertFalse(hasattr(request, "key"))
        self.answers.append(Answer(403, {"error": {"message": "Invalid code"}}))
        with self.assertRaises(DomainError) as caught:
            self.client.exchange(CODE, "verifier")
        self.assertEqual(caught.exception.code, "openrouter_key_refused")

    def test_the_keys_limit_and_use_are_read_with_the_key(self):
        self.answers.append(Answer(200, {"data": {"label": "made-up label", "limit": 10, "limit_remaining": 7.5, "usage": 2.5,
                                                  "is_free_tier": False}}))
        self.assertEqual(self.client.key_info(KEY), {"limit_usd": 10, "remaining_usd": 7.5, "used_usd": 2.5, "free_tier": False})
        self.assertEqual((self.sent[0].path, self.sent[0].key), ("/api/v1/key", KEY))
        self.answers.append(Answer(200, {"data": {"limit": None, "limit_remaining": None, "usage": 0}}))
        self.assertEqual(self.client.key_info(KEY)["limit_usd"], None)
        self.answers.append(Answer(401, {"error": {"message": "No auth"}}))
        with self.assertRaises(DomainError) as caught:
            self.client.key_info(KEY)
        self.assertEqual(caught.exception.code, "openrouter_key_refused")

    def test_a_recording_is_uploaded_as_a_form_with_the_model_and_its_text_and_cost_come_back(self):
        self.answers.append(Answer(200, {"text": " hello there ", "usage": {"seconds": 3.2, "cost": 0.0004}}))
        answer = self.client.transcribe(KEY, b"\x00\x01audio", media_type="audio/mp4", model="openai/whisper-1", language="en",
                                        provider="deepinfra")
        self.assertEqual(answer, {"text": "hello there", "cost_usd": 0.0004, "seconds": 3.2})
        request, = self.sent
        self.assertEqual((request.method, request.path, request.key), ("POST", "/api/v1/audio/transcriptions", KEY))
        boundary = request.content_type.split("boundary=", 1)[1]
        self.assertTrue(request.content_type.startswith("multipart/form-data;"))
        self.assertIn(b'name="model"\r\n\r\nopenai/whisper-1\r\n', request.data)
        self.assertIn(b'name="language"\r\n\r\nen\r\n', request.data)
        self.assertIn(b'name="provider"\r\n\r\n{"order":["deepinfra"]}\r\n', request.data)
        self.assertIn(b'name="file"; filename="recording.m4a"\r\nContent-Type: audio/mp4\r\n\r\n\x00\x01audio\r\n', request.data)
        self.assertTrue(request.data.endswith(f"--{boundary}--\r\n".encode()))

    def test_openrouters_refusals_are_explained(self):
        for status, body, words in ((401, {}, "did not accept the saved key"), (402, {}, "balance is too low"),
                                    (400, {"error": {"message": "Audio too  short"}}, "Audio too short"),
                                    (500, "oops", "Try again in a moment")):
            self.answers.append(Answer(status, body))
            with self.assertRaises(DomainError) as caught:
                self.client.transcribe(KEY, b"audio", media_type="audio/webm", model="openai/whisper-1")
            self.assertEqual(caught.exception.code, "operation_unavailable")
            self.assertIn(words, caught.exception.message)

    def test_a_form_upload_has_one_part_per_field_and_the_file_last(self):
        content_type, body = multipart({"model": "m"}, file_name="recording.webm", file_type="audio/webm", file_bytes=b"abc",
                                       boundary="edge")
        self.assertEqual(content_type, "multipart/form-data; boundary=edge")
        self.assertEqual(body, b'--edge\r\nContent-Disposition: form-data; name="model"\r\n\r\nm\r\n'
                               b'--edge\r\nContent-Disposition: form-data; name="file"; filename="recording.webm"\r\n'
                               b"Content-Type: audio/webm\r\n\r\nabc\r\n--edge--\r\n")


if __name__ == "__main__":
    unittest.main()
