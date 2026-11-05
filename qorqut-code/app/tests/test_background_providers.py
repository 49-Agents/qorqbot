import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from bazarlyk.background.providers import (
    BackgroundModelClient, ProviderConfig, ProviderError, call_cost, is_peak, usage_of,
)


def response(content="A bounded summary."):
    return io.BytesIO(json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }).encode())


def test_chat_preserves_background_contract_and_does_not_execute_tools():
    calls = []

    def open_request(request, **kwargs):
        calls.append((request, kwargs))
        return response()

    config = ProviderConfig("secret", base_url="https://provider.example/v1/", timeout=17)
    client = BackgroundModelClient(config, opener=open_request)
    messages = [{"role": "user", "content": "Summarize the supplied source."}]
    tools = [{"type": "function", "function": {"name": "propose_note"}}]
    message, usage = client.chat(messages, tools=tools, thinking=False, max_tokens=90)
    request, kwargs = calls[0]
    body = json.loads(request.data)
    assert request.full_url == "https://provider.example/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer secret"
    assert kwargs == {"timeout": 17}
    assert body["tools"] == tools
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 90
    assert body["temperature"] == 0.3
    assert "response_format" not in body
    assert message["_finish"] == "stop"
    assert usage == {"prompt_tokens": 12, "completion_tokens": 4}
    assert messages == [{"role": "user", "content": "Summarize the supplied source."}]
    assert "secret" not in repr(config)


def test_learning_json_mode_and_temperature_are_explicit_per_call():
    bodies = []

    def opener(request, **_):
        bodies.append(json.loads(request.data))
        return response('{"items":[]}')

    client = BackgroundModelClient(ProviderConfig("test"), opener=opener)
    requested = {"type": "json_object"}
    client.chat([{"role": "user", "content": "Return JSON."}], response_format=requested, temperature=0.1)
    assert bodies[0]["response_format"] == requested
    assert bodies[0]["temperature"] == 0.1
    assert requested == {"type": "json_object"}


@pytest.mark.parametrize("arguments", [
    {"temperature": -0.1}, {"temperature": 2.1}, {"temperature": True},
    {"temperature": "0.1"}, {"temperature": float("nan")},
    {"response_format": "json"}, {"response_format": {"type": "json_object", "unknown": True}},
])
def test_invalid_learning_provider_options_never_send_a_request(arguments):
    client = BackgroundModelClient(ProviderConfig("test"), opener=lambda *args, **kwargs: pytest.fail("request sent"))
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}], **arguments)
    assert caught.value.code == "invalid_request"


def test_two_business_clients_keep_distinct_credentials_and_environment():
    values = [{"DEEPSEEK_API_KEY": "business-a", "BIZ_BACKGROUND_MODEL": "model-a"},
              {"DEEPSEEK_API_KEY": "business-b", "BIZ_BACKGROUND_MODEL": "model-b"}]

    def run(environment):
        def open_request(request, **kwargs):
            return response(request.get_header("Authorization") + ":" + json.loads(request.data)["model"])
        client = BackgroundModelClient(ProviderConfig.from_environment(environment), opener=open_request)
        return client.chat([{"role": "user", "content": "test"}])[0]["content"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, values)) == ["Bearer business-a:model-a", "Bearer business-b:model-b"]
    assert values[0]["DEEPSEEK_API_KEY"] == "business-a"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_failure_retries_with_bound(status):
    calls, sleeps = [], []

    def open_request(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, status, "busy", {}, io.BytesIO(b"busy"))
        return response()

    client = BackgroundModelClient(ProviderConfig("key"), opener=open_request, sleep=sleeps.append)
    client.chat([{"role": "user", "content": "test"}])
    assert len(calls) == 2
    assert sleeps == [3]


def test_unsupported_thinking_retries_without_mutating_other_fields():
    bodies = []

    def open_request(request, **kwargs):
        bodies.append(json.loads(request.data))
        if len(bodies) == 1:
            raise urllib.error.HTTPError(request.full_url, 400, "bad field", {},
                                         io.BytesIO(b"thinking is not supported"))
        return response()

    client = BackgroundModelClient(ProviderConfig("key"), opener=open_request)
    client.chat([{"role": "user", "content": "test"}], thinking=False)
    assert bodies[0].pop("thinking") == {"type": "disabled"}
    assert bodies[0] == bodies[1]


def test_errors_do_not_expose_provider_payload_or_credentials():
    def open_request(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {},
                                     io.BytesIO(b"secret-provider-key and private prompt"))

    client = BackgroundModelClient(ProviderConfig("secret-provider-key"), opener=open_request)
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}])
    assert str(caught.value) == "Background provider returned HTTP 401."
    assert caught.value.status == 401
    assert not caught.value.retryable


def test_transport_failure_stops_after_configured_attempts():
    calls = []

    def open_request(request, **kwargs):
        calls.append(request)
        raise TimeoutError("private endpoint")

    client = BackgroundModelClient(ProviderConfig("key"), opener=open_request, sleep=lambda _: None)
    with pytest.raises(ProviderError, match="unavailable") as caught:
        client.chat([{"role": "user", "content": "test"}])
    assert caught.value.retryable
    assert caught.value.usage is None
    assert caught.value.model is None
    assert len(calls) == 2


def test_job_budget_caps_each_attempt_and_includes_retry_delay():
    elapsed, timeouts = [0.0], []

    def sleep(seconds):
        elapsed[0] += seconds

    def opener(request, *, timeout):
        timeouts.append(timeout)
        elapsed[0] += 10 if len(timeouts) == 1 else 1
        if len(timeouts) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, io.BytesIO(b"busy"))
        return response()

    client = BackgroundModelClient(ProviderConfig("key"), opener=opener, sleep=sleep, monotonic=lambda: elapsed[0])
    client.chat([{"role": "user", "content": "test"}], timeout_budget=30)
    assert timeouts == [30, 17]
    assert elapsed[0] == 14
    assert client.config.timeout == 180


@pytest.mark.parametrize("budget", [0, -1])
def test_expired_job_budget_never_sends_a_request(budget):
    client = BackgroundModelClient(ProviderConfig("key"), opener=lambda *a, **kw: pytest.fail("request sent"))
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}], timeout_budget=budget)
    assert caught.value.code == "provider_timeout"
    assert not caught.value.retryable


@pytest.mark.parametrize("budget", [True, "30", float("inf"), float("nan")])
def test_invalid_job_budget_is_rejected(budget):
    client = BackgroundModelClient(ProviderConfig("key"), opener=lambda *a, **kw: pytest.fail("request sent"))
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}], timeout_budget=budget)
    assert caught.value.code == "invalid_request"


def test_retry_pause_cannot_extend_the_remaining_job_budget():
    elapsed, calls, sleeps = [0.0], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    def opener(request, *, timeout):
        calls.append(timeout)
        elapsed[0] += 8
        raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, io.BytesIO(b"private"))

    client = BackgroundModelClient(ProviderConfig("key", retry_delay=5), opener=opener, sleep=sleep, monotonic=lambda: elapsed[0])
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}], timeout_budget=10)
    assert caught.value.code == "provider_timeout"
    assert calls == [10]
    assert sleeps == [2]


def test_transport_exhaustion_does_not_start_another_attempt():
    elapsed, calls = [0.0], []

    def opener(request, *, timeout):
        calls.append(timeout)
        elapsed[0] += timeout
        raise TimeoutError("private endpoint")

    client = BackgroundModelClient(ProviderConfig("key"), opener=opener,
        sleep=lambda _: pytest.fail("retry slept after deadline"), monotonic=lambda: elapsed[0])
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}], timeout_budget=60)
    assert caught.value.code == "provider_timeout"
    assert caught.value.usage is None
    assert caught.value.model is None
    assert calls == [60]


def test_late_provider_success_cannot_become_a_job_output():
    elapsed = [0.0]

    def opener(request, *, timeout):
        elapsed[0] += timeout + 1
        return response("Late output")

    client = BackgroundModelClient(ProviderConfig("key"), opener=opener, monotonic=lambda: elapsed[0])
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}], model="deepseek-v4-pro", timeout_budget=5)
    assert caught.value.code == "provider_timeout"
    assert caught.value.usage == {"prompt_tokens": 12, "completion_tokens": 4}
    assert caught.value.model == "deepseek-v4-pro"
    assert "Late output" not in str(caught.value)


def test_thinking_fallback_keeps_the_original_deadline():
    elapsed, timeouts = [0.0], []

    def opener(request, *, timeout):
        timeouts.append(timeout)
        elapsed[0] += 4
        if len(timeouts) == 1:
            raise urllib.error.HTTPError(request.full_url, 400, "unsupported", {}, io.BytesIO(b"thinking"))
        return response()

    client = BackgroundModelClient(ProviderConfig("key"), opener=opener, monotonic=lambda: elapsed[0])
    client.chat([{"role": "user", "content": "test"}], thinking=False, timeout_budget=10)
    assert timeouts == [10, 6]


@pytest.mark.parametrize("stop_at", ["before", "retry"])
def test_job_cancellation_propagates_without_becoming_a_provider_failure(stop_at):
    stopped, calls, sleeps = [stop_at == "before"], [], []

    class JobStopped(ValueError):
        pass

    def check():
        if stopped[0]:
            raise JobStopped("Owner cancelled the job")

    def opener(request, **kwargs):
        calls.append(request)
        stopped[0] = True
        if stop_at == "retry":
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, io.BytesIO(b"busy"))
        return response()

    client = BackgroundModelClient(ProviderConfig("key"), opener=opener, sleep=sleeps.append)
    with pytest.raises(JobStopped):
        client.chat([{"role": "user", "content": "test"}], check_cancelled=check)
    assert len(calls) == (0 if stop_at == "before" else 1)
    assert sleeps == []


def test_successful_response_returns_usage_before_wrapper_checks_cancellation():
    stopped = [False]

    def check():
        if stopped[0]:
            pytest.fail("Usage was discarded before the background wrapper could record it")

    def opener(request, **kwargs):
        stopped[0] = True
        return response()

    client = BackgroundModelClient(ProviderConfig("key"), opener=opener)
    _, usage = client.chat([{"role": "user", "content": "test"}], check_cancelled=check)
    assert usage == {"prompt_tokens": 12, "completion_tokens": 4}


def test_http_error_body_failure_still_closes_response_and_keeps_safe_error():
    class BrokenBody(io.BytesIO):
        def read(self, *args):
            raise TimeoutError("Private endpoint")

    body = BrokenBody()

    def opener(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, body)

    client = BackgroundModelClient(ProviderConfig("key", attempts=1), opener=opener)
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}])
    assert caught.value.code == "provider_http_error"
    assert "Private" not in str(caught.value)
    assert body.closed


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}", b'{"choices":[]}',
                                    b'{"choices":[{"message":null}]}'])
def test_invalid_response_is_an_explicit_failure(body):
    client = BackgroundModelClient(ProviderConfig("key"), opener=lambda *a, **k: io.BytesIO(body))
    with pytest.raises(ProviderError) as caught:
        client.chat([{"role": "user", "content": "test"}])
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize("config", [
    {"api_key": ""}, {"api_key": "bad\nheader"},
    {"api_key": "key", "base_url": "file:///etc/passwd"},
    {"api_key": "key", "base_url": "https://user:pass@host.example"},
    {"api_key": "key", "base_url": "https://host.example?token=secret"},
    {"api_key": "key", "attempts": 0}, {"api_key": "key", "attempts": True},
    {"api_key": "key", "timeout": 0},
    {"api_key": "key", "timeout": float("inf")},
    {"api_key": "key", "timeout": None},
    {"api_key": "key", "retry_delay": float("nan")},
])
def test_invalid_configuration_is_rejected(config):
    with pytest.raises(ProviderError):
        ProviderConfig(**config)


def test_usage_preserves_existing_cache_and_older_shapes():
    assert usage_of({"prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 3,
                     "completion_tokens": 2}) == {"hit": 5, "miss": 3, "out": 2}
    assert usage_of({"prompt_tokens": 9, "completion_tokens": 4}) == {"hit": 0, "miss": 9, "out": 4}
    assert usage_of(None) == {"hit": 0, "miss": 0, "out": 0}
    with pytest.raises(ProviderError):
        usage_of({"prompt_tokens": -1})


def test_cost_preserves_configured_rates_and_utc_windows():
    peak = datetime(2026, 9, 8, 2, tzinfo=timezone.utc)
    off_peak = peak.replace(hour=12)
    assert is_peak(peak)
    assert is_peak(peak.astimezone(timezone(timedelta(hours=-7))))
    assert not is_peak(off_peak)
    assert not is_peak(peak.replace(day=12))
    assert call_cost("deepseek-v4-flash", 1_000_000, 1_000_000, 1_000_000, peak) == 1.774
    assert call_cost("deepseek-v4-flash", 1_000_000, 1_000_000, 1_000_000, off_peak) == 0.887
    assert call_cost("unknown", 1_000_000, 0, 0, peak) == 0.014
    assert call_cost("test", 1_000_000, 0, 0, peak, rates={"test": (2, 3, 4)}) == 2
    with pytest.raises(ValueError):
        is_peak(datetime(2026, 9, 8, 2))
