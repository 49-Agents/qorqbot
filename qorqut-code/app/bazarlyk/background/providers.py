"""Explicit, independently testable model access for background jobs only."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.account_proxy import RequiredAccountProxy


DEFAULT_MODEL = "deepseek-v4-flash"
# Preserve the deployed cost estimates; these are configuration, not live prices.
RATES = {
    "deepseek-v4-flash": (0.014, 0.44, 1.32),
    "deepseek-v4-pro": (0.044, 1.32, 3.96),
}
PEAK_WINDOWS = ((1, 4), (6, 10))
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 status: int | None = None, usage: dict | None = None,
                 model: str | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status
        self.usage = usage
        self.model = model


@dataclass(frozen=True)
class ProviderConfig:
    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = DEFAULT_MODEL
    timeout: float = 180
    attempts: int = 2
    retry_delay: float = 3

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ProviderError("provider_not_configured", "Background provider key is missing.")
        if "\r" in self.api_key or "\n" in self.api_key:
            raise ProviderError("provider_not_configured", "Background provider key is invalid.")
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme in {"http", "https"} and url.hostname
                     and not url.username and not url.password
                     and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ProviderError("provider_not_configured", "Background provider URL is invalid.")
        if (not isinstance(self.model, str) or not self.model.strip()
                or isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or self.timeout <= 0
                or isinstance(self.retry_delay, bool) or not isinstance(self.retry_delay, (int, float))
                or not math.isfinite(self.retry_delay) or self.retry_delay < 0):
            raise ProviderError("provider_not_configured", "Background provider settings are invalid.")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or not 1 <= self.attempts <= 3:
            raise ProviderError("provider_not_configured", "Background provider attempts must be 1 to 3.")

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> "ProviderConfig":
        """The caller supplies its business-specific environment; never mutate it."""
        return cls(
            api_key=values.get("DEEPSEEK_API_KEY", ""),
            base_url=values.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=values.get("BIZ_BACKGROUND_MODEL", DEFAULT_MODEL),
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials are bound to the configured provider, not redirect targets.
        return None


class BackgroundModelClient:
    def __init__(self, config: ProviderConfig, *, opener: Callable | None = None,
                 proxy: str | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.config = config
        if proxy is not None and urlsplit(config.base_url).scheme != "https":
            # urllib's ProxyHandler only installs an HTTPS CONNECT route here;
            # do not leave an HTTP provider request with a direct-handler path.
            raise DomainError("invalid_configuration", "Custom sandbox model provider URL must use HTTPS")
        # A custom selected-business destination must use this explicit handler;
        # it overrides ambient NO_PROXY rather than falling back to host egress.
        self._open = opener or urllib.request.build_opener(
            _NoRedirect(), *([RequiredAccountProxy(proxy)] if proxy is not None else [])).open
        self._sleep = sleep
        self._monotonic = monotonic

    def chat(self, messages: Sequence[Mapping[str, Any]], model: str | None = None,
             tools: Sequence[Mapping[str, Any]] | None = None,
             max_tokens: int = 40000, thinking: bool | None = None, *,
             response_format: Mapping[str, Any] | None = None,
             temperature: float = 0.3,
             timeout_budget: float | None = None,
             check_cancelled: Callable[[], None] | None = None) -> tuple[dict, dict]:
        """One completion with bounded transport retries; no agent loop or tools execute."""
        if not messages or isinstance(messages, (str, bytes)):
            raise ProviderError("invalid_request", "A background prompt is required.")
        if any(not isinstance(item, Mapping) for item in messages):
            raise ProviderError("invalid_request", "Background messages must be objects.")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ProviderError("invalid_request", "The output token limit must be positive.")
        if thinking is not None and not isinstance(thinking, bool):
            raise ProviderError("invalid_request", "Thinking must be a boolean or unset.")
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ProviderError("invalid_request", "Temperature must be a finite number from 0 to 2.")
        if response_format is not None and response_format not in ({"type": "json_object"}, {"type": "text"}):
            raise ProviderError("invalid_request", "Background response format must be json_object or text.")
        if timeout_budget is not None and (isinstance(timeout_budget, bool)
                or not isinstance(timeout_budget, (int, float)) or not math.isfinite(timeout_budget)):
            raise ProviderError("invalid_request", "Background timeout budget must be a finite number.")
        if check_cancelled is not None and not callable(check_cancelled):
            raise ProviderError("invalid_request", "Background cancellation check must be callable.")
        deadline = self._monotonic() + timeout_budget if timeout_budget is not None else None

        def remaining():
            if deadline is None:
                return None
            value = deadline - self._monotonic()
            if value <= 0:
                raise ProviderError("provider_timeout", "Background provider exhausted the job time budget.")
            return value

        def retry_pause():
            if check_cancelled:
                check_cancelled()
            budget = remaining()
            delay = self.config.retry_delay if budget is None else min(self.config.retry_delay, budget)
            self._sleep(delay)
            if check_cancelled:
                check_cancelled()
            remaining()

        remaining()
        body: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": [dict(item) for item in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            body["response_format"] = dict(response_format)
        if tools:
            body["tools"] = [dict(item) for item in tools]
            body["tool_choice"] = "auto"
        if thinking is not None:
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        for attempt in range(self.config.attempts):
            if check_cancelled:
                check_cancelled()
            try:
                encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ProviderError("invalid_request", "Background request is not valid JSON.") from exc
            request = urllib.request.Request(
                self.config.base_url.rstrip("/") + "/chat/completions",
                data=encoded,
                headers={"Authorization": f"Bearer {self.config.api_key}",
                         "Content-Type": "application/json"},
            )
            can_retry = attempt + 1 < self.config.attempts
            try:
                budget = remaining()
                timeout = self.config.timeout if budget is None else min(self.config.timeout, budget)
                with self._open(request, timeout=timeout) as response:
                    data = json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    error_body = exc.read(4096).decode("utf-8", errors="replace")
                except OSError:
                    error_body = ""
                finally:
                    exc.close()
                if check_cancelled:
                    check_cancelled()
                remaining()
                if (can_retry and exc.code == 400 and "thinking" in body
                        and "thinking" in error_body.lower()):
                    body.pop("thinking")
                    continue
                if can_retry and exc.code in RETRYABLE_STATUSES:
                    retry_pause()
                    continue
                raise ProviderError(
                    "provider_http_error", f"Background provider returned HTTP {exc.code}.",
                    retryable=exc.code in RETRYABLE_STATUSES, status=exc.code,
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if check_cancelled:
                    check_cancelled()
                remaining()
                if can_retry:
                    retry_pause()
                    continue
                raise ProviderError("provider_unreachable", "Background provider is unavailable.",
                                    retryable=True) from None
            except (ValueError, UnicodeError) as exc:
                remaining()
                raise ProviderError("invalid_response", "Background provider returned invalid JSON.") from exc
            else:
                message, usage = self._completion(data)
                try:
                    remaining()
                except ProviderError as exc:
                    # A late completion is unusable, but its reported usage is billable.
                    exc.usage = usage
                    exc.model = body["model"]
                    raise
                return message, usage
        raise AssertionError("Validated retry count must produce a result or error.")

    @staticmethod
    def _completion(data: Any) -> tuple[dict, dict]:
        if not isinstance(data, dict):
            raise ProviderError("invalid_response", "Background response must be an object.")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("invalid_response", "Background response has no completion.")
        choice = choices[0]
        message = choice.get("message")
        usage = data.get("usage") or {}
        if not isinstance(message, dict) or not isinstance(usage, dict):
            raise ProviderError("invalid_response", "Background completion shape is invalid.")
        message = dict(message)
        message["_finish"] = choice.get("finish_reason")
        return message, dict(usage)


def usage_of(raw_usage: Mapping[str, Any] | None) -> dict[str, int]:
    raw = raw_usage or {}

    def count(key: str) -> int:
        try:
            value = int(raw.get(key) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderError("invalid_usage", "Background provider token usage is invalid.") from exc
        if value < 0:
            raise ProviderError("invalid_usage", "Background provider token usage is negative.")
        return value

    if "prompt_cache_hit_tokens" in raw or "prompt_cache_miss_tokens" in raw:
        return {"hit": count("prompt_cache_hit_tokens"),
                "miss": count("prompt_cache_miss_tokens"), "out": count("completion_tokens")}
    return {"hit": 0, "miss": count("prompt_tokens"), "out": count("completion_tokens")}


def is_peak(when: datetime | None = None) -> bool:
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ValueError("Cost timestamps must include a timezone.")
    utc = when.astimezone(timezone.utc)
    hour = utc.hour + utc.minute / 60 + utc.second / 3600
    return utc.weekday() < 5 and any(start <= hour < end for start, end in PEAK_WINDOWS)


def call_cost(model: str, hit: int, miss: int, out: int, when: datetime | None = None,
              *, rates: Mapping[str, tuple[float, float, float]] | None = None) -> float:
    """Configured USD estimate, preserving existing fallback and UTC peak behavior."""
    configured = rates if rates is not None else RATES
    rate = configured.get(model, configured.get(DEFAULT_MODEL))
    if rate is None:
        raise ValueError("No configured rate exists for the model or fallback.")
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (hit, miss, out)):
        raise ValueError("Token counts must be nonnegative integers.")
    multiplier = 1.0 if is_peak(when) else 0.5
    return round(sum(n * price for n, price in zip((hit, miss, out), rate))
                 * multiplier / 1_000_000, 8)
