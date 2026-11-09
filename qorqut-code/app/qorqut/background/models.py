"""The single provider adapter, with durable usage and bounded structured replies."""

import json

from qorqut_operations.contracts import DomainError
from .providers import ProviderError, call_cost, usage_of


class ModelCalls:
    def __init__(self, factory):
        self.factory = factory

    def ask(self, control, system, user, *, json_object=False, max_tokens=40000,
            thinking=None, model=None, empty_retries=1):
        if self.factory is None:
            raise DomainError("operation_unavailable", "Background model provider is not configured")
        try:
            client = self.factory(control.business_id)
        except ProviderError as exc:
            raise DomainError(exc.code, str(exc)) from exc
        model = model or client.config.model
        for attempt in range(empty_retries + 1):
            control.check()
            try:
                message, raw = client.chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": user + ("\nAnswer directly; keep reasoning short." if attempt else "")},
                ], model=model, max_tokens=max_tokens, thinking=thinking,
                    response_format={"type": "json_object"} if json_object else None,
                    temperature=0.1 if json_object else 0.3, timeout_budget=control.remaining_seconds(), check_cancelled=control.check)
            except ProviderError as exc:
                if exc.usage is not None:
                    usage = usage_of(exc.usage)
                    billed_model = exc.model or model
                    control.record_usage(billed_model, usage, float(call_cost(billed_model, usage["hit"], usage["miss"], usage["out"])))
                raise DomainError(exc.code, str(exc)) from exc
            usage = usage_of(raw or {})
            cost = float(call_cost(model, usage["hit"], usage["miss"], usage["out"]))
            control.record_usage(model, usage, cost)
            control.check()
            content = (message or {}).get("content")
            if isinstance(content, str) and content.strip():
                if not json_object:
                    return content.strip(), usage
                try:
                    value = json.loads(content)
                except (ValueError, TypeError) as exc:
                    raise DomainError("invalid_model_output", "Background model did not return valid JSON") from exc
                if not isinstance(value, dict):
                    raise DomainError("invalid_model_output", "Background model JSON must be an object")
                return value, usage
        raise DomainError("empty_model_output", "Background model returned no usable answer")
