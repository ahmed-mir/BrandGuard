"""Provider-specific clients for the shared, validated advisory response contract."""
import json
import re

from django.conf import settings

from .schemas import FINDING_SCHEMA, SYSTEM_INSTRUCTIONS


class ProviderFailure(Exception):
    def __init__(self, category, diagnostics=None):
        self.category = category
        self.diagnostics = diagnostics or {}
        super().__init__(category)


_SAFE_STATUSES = {"completed", "incomplete", "failed", "queued", "in_progress", "cancelled"}
_SAFE_ITEM_TYPES = {"message", "reasoning", "function_call", "web_search_call", "other"}
_SAFE_PARSER_STAGES = {
    "provider_http_error", "response_status", "incomplete_response", "refusal", "missing_text",
    "json_decode", "provider_schema_contract", "domain_parse", "domain_semantic",
    "schema_validation", "evidence_validation", "unsupported_response_shape",
}
_SAFE_INCOMPLETE_REASONS = {"max_output_tokens", "content_filter", "other"}
_SAFE_CHAT_FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls", "function_call", "other"}


def _safe_model_identifier(model):
    """Keep only a bounded model identifier; never accept arbitrary response text."""
    return re.sub(r"[^A-Za-z0-9._/-]", "", str(model or ""))[:64]


def _response_diagnostics(response, *, provider, model, parser_stage, provider_status_code=None):
    """Return bounded metadata only. No prompt, content, headers, or response text is retained."""
    raw_status = getattr(response, "status", None)
    status = raw_status if isinstance(raw_status, str) and raw_status in _SAFE_STATUSES else "unknown"
    output = getattr(response, "output", None) or []
    item_types = []
    for item in output[:8]:
        raw_type = getattr(item, "type", None)
        item_types.append(raw_type if isinstance(raw_type, str) and raw_type in _SAFE_ITEM_TYPES else "other")
    output_text = getattr(response, "output_text", None)
    incomplete_details = getattr(response, "incomplete_details", None)
    raw_reason = getattr(incomplete_details, "reason", None) if incomplete_details else None
    reason = raw_reason if isinstance(raw_reason, str) and raw_reason in _SAFE_INCOMPLETE_REASONS else ("other" if incomplete_details else "")
    diagnostics = {
        "provider": provider if provider in {"openai", "groq"} else "unknown",
        "model": _safe_model_identifier(model),
        "response_status": status,
        "output_item_types": item_types,
        "output_text_present": isinstance(output_text, str),
        "output_incomplete": status == "incomplete" or incomplete_details is not None,
    }
    if parser_stage:
        diagnostics["parser_stage"] = parser_stage if parser_stage in _SAFE_PARSER_STAGES else "unsupported_response_shape"
    if reason:
        diagnostics["incomplete_reason"] = reason
    if isinstance(provider_status_code, int) and 400 <= provider_status_code <= 599:
        diagnostics["provider_status_code"] = provider_status_code
    return diagnostics


def _extract_output_text(response):
    """Prefer the SDK's Responses output_text helper, with a message-only fallback."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    # Some compatible Responses implementations omit the SDK convenience field.
    # Ignore reasoning and other output items; only assistant message text is parseable.
    texts = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) in {"output_text", "text"}:
                text = getattr(block, "text", None)
                if isinstance(text, str) and text.strip():
                    texts.append(text)
    return "\n".join(texts)


def _request_payload_strings(value):
    if isinstance(value, str):
        if len(value) >= 6:
            yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _request_payload_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _request_payload_strings(child)


def _safe_provider_message(message, payload):
    """Bound provider diagnostics and redact exact user/source text and secret-like tokens."""
    if not isinstance(message, str):
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", message)
    cleaned = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", cleaned)
    cleaned = re.sub(r"\b(?:gsk|sk)-?[A-Za-z0-9_-]{16,}\b", "[redacted credential]", cleaned)
    for value in sorted(set(_request_payload_strings(payload)), key=len, reverse=True):
        cleaned = re.sub(re.escape(value), "[request text redacted]", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()[:400]


def _chat_diagnostics(response, *, model, finish_reason=None, parser_stage=None, content=None, status_code=None,
                      error_type=None, error_message=None):
    choices = getattr(response, "choices", None) or []
    status = "incomplete" if finish_reason == "length" else "completed" if finish_reason == "stop" else "unknown"
    details = {
        "provider": "groq",
        "model": _safe_model_identifier(model),
        "response_status": status,
        "output_item_types": [],
        "output_text_present": isinstance(content, str),
        "output_incomplete": finish_reason == "length",
    }
    if finish_reason:
        details["finish_reason"] = finish_reason if finish_reason in _SAFE_CHAT_FINISH_REASONS else "other"
    if finish_reason == "length":
        details["incomplete_reason"] = "max_output_tokens"
    if parser_stage:
        details["parser_stage"] = parser_stage if parser_stage in _SAFE_PARSER_STAGES else "unsupported_response_shape"
    if isinstance(status_code, int) and not isinstance(status_code, bool) and 400 <= status_code <= 599:
        details["provider_status_code"] = status_code
    if isinstance(error_type, str):
        details["provider_error_type"] = re.sub(r"[^A-Za-z0-9_.-]", "", error_type)[:64]
    if isinstance(error_message, str) and error_message:
        details["provider_message"] = error_message[:400]
    if not choices and not parser_stage:
        details["parser_stage"] = "unsupported_response_shape"
    return details


def _groq_http_failure(exc, *, model, payload):
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    error_type = error.get("type")
    error_message = _safe_provider_message(error.get("message"), payload)
    diagnostics = _chat_diagnostics(
        None,
        model=model,
        parser_stage="provider_http_error",
        status_code=status_code,
        error_type=error_type,
        error_message=error_message,
    )
    if status_code == 429:
        category = _rate_limit_category(exc)
    elif status_code in {401, 403}:
        category = "AUTHENTICATION"
    elif status_code in {400, 413, 422}:
        category = "INVALID_RESPONSE"
    elif status_code == 404 or (isinstance(status_code, int) and status_code >= 500):
        category = "PROVIDER_UNAVAILABLE"
    else:
        category = "INVALID_RESPONSE"
    return ProviderFailure(category, diagnostics)


def _request_groq_chat(client, payload, model):
    """Groq Chat Completions transport; normalization is shared with BrandGuard's validator."""
    from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, RateLimitError

    request_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "brandguard_advisory",
                "strict": True,
                "schema": FINDING_SCHEMA,
            },
        },
        "reasoning_effort": "low",
        "max_completion_tokens": settings.BRANDGUARD_AI_MAX_OUTPUT_TOKENS,
        "extra_body": {"include_reasoning": False},
        "stream": False,
        "timeout": settings.BRANDGUARD_AI_TIMEOUT_SECONDS,
    }
    try:
        response = client.chat.completions.create(**request_kwargs)
    except APITimeoutError as exc:
        raise ProviderFailure("TIMEOUT", {
            "provider": "groq", "model": _safe_model_identifier(model), "parser_stage": "provider_http_error",
        }) from exc
    except AuthenticationError as exc:
        raise _groq_http_failure(exc, model=model, payload=payload) from exc
    except RateLimitError as exc:
        raise _groq_http_failure(exc, model=model, payload=payload) from exc
    except APIStatusError as exc:
        raise _groq_http_failure(exc, model=model, payload=payload) from exc
    except APIConnectionError as exc:
        raise ProviderFailure("PROVIDER_UNAVAILABLE", {
            "provider": "groq", "model": _safe_model_identifier(model), "parser_stage": "provider_http_error",
        }) from exc

    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ProviderFailure("INVALID_RESPONSE", _chat_diagnostics(
            response, model=model, parser_stage="unsupported_response_shape",
        ))
    choice = choices[0]
    message = getattr(choice, "message", None)
    finish_reason = getattr(choice, "finish_reason", None)
    content = getattr(message, "content", None) if message else None
    if getattr(message, "refusal", None):
        raise ProviderFailure("REFUSAL", _chat_diagnostics(
            response, model=model, finish_reason=finish_reason, parser_stage="refusal", content=content,
        ))
    if finish_reason == "length":
        raise ProviderFailure("INVALID_RESPONSE", _chat_diagnostics(
            response, model=model, finish_reason=finish_reason, parser_stage="incomplete_response", content=content,
        ))
    if not isinstance(content, str) or not content.strip():
        stage = "missing_text" if message is not None else "unsupported_response_shape"
        raise ProviderFailure("INVALID_RESPONSE", _chat_diagnostics(
            response, model=model, finish_reason=finish_reason, parser_stage=stage, content=content,
        ))
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ProviderFailure("INVALID_RESPONSE", _chat_diagnostics(
            response, model=model, finish_reason=finish_reason, parser_stage="json_decode", content=content,
        )) from exc
    usage = getattr(response, "usage", None)
    return {
        "data": parsed,
        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "diagnostics": _chat_diagnostics(response, model=model, finish_reason=finish_reason, content=content),
    }


def _rate_limit_category(exc):
    """Read only the provider's safe machine code; never surface its response body."""
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if not code and isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            code = error.get("code")
    return "QUOTA" if code == "insufficient_quota" else "PROVIDER_RATE_LIMIT"


def request_analysis(payload, *, provider="openai", model=None):
    """Send one explicitly authorized provider request and return JSON plus usage."""
    if not settings.BRANDGUARD_AI_ENABLED:
        raise ProviderFailure("AI_DISABLED")
    if settings.BRANDGUARD_AI_DEMO_MODE:
        raise ProviderFailure("MODE_CONFLICT")
    if provider != settings.BRANDGUARD_AI_PROVIDER:
        raise ProviderFailure("INVALID_PROVIDER")
    if provider == "openai":
        api_key = settings.OPENAI_API_KEY
        base_url = None
        model = model or settings.BRANDGUARD_AI_MODEL
    elif provider == "groq":
        api_key = settings.GROQ_API_KEY
        base_url = "https://api.groq.com/openai/v1"
        model = model or settings.BRANDGUARD_GROQ_MODEL
    else:
        raise ProviderFailure("INVALID_PROVIDER")
    if not api_key:
        raise ProviderFailure("MISSING_API_KEY")

    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        OpenAI,
        RateLimitError,
    )

    client_kwargs = {
        "api_key": api_key,
        "timeout": settings.BRANDGUARD_AI_TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    if provider == "groq":
        return _request_groq_chat(client, payload, model)

    # Preserve OpenAI's existing Responses API request contract.
    request_kwargs = {
        "model": model,
        "instructions": SYSTEM_INSTRUCTIONS,
        "input": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "brandguard_advisory_analysis",
                "strict": True,
                "schema": FINDING_SCHEMA,
            }
        },
        "max_output_tokens": settings.BRANDGUARD_AI_MAX_OUTPUT_TOKENS,
    }
    # Keep this OpenAI Responses-only privacy option on the OpenAI request path.
    if provider == "openai":
        request_kwargs["store"] = False
    try:
        response = client.responses.create(**request_kwargs)
    except APITimeoutError as exc:
        raise ProviderFailure("TIMEOUT") from exc
    except AuthenticationError as exc:
        raise ProviderFailure("AUTHENTICATION") from exc
    except RateLimitError as exc:
        # Distinguish only the SDK's documented status class; never persist its raw body.
        raise ProviderFailure(_rate_limit_category(exc)) from exc
    except APIStatusError as exc:
        diagnostics = {
            "provider": provider,
            "model": _safe_model_identifier(model),
            "parser_stage": "provider_http_error",
            "provider_status_code": exc.status_code,
        }
        if exc.status_code == 429:
            raise ProviderFailure(_rate_limit_category(exc), diagnostics) from exc
        if 400 <= exc.status_code < 500:
            raise ProviderFailure("INVALID_RESPONSE", diagnostics) from exc
        raise ProviderFailure("PROVIDER_UNAVAILABLE") from exc
    except APIConnectionError as exc:
        raise ProviderFailure("PROVIDER_UNAVAILABLE") from exc

    status = getattr(response, "status", "completed")
    incomplete_details = getattr(response, "incomplete_details", None)
    if status != "completed":
        stage = "incomplete_response" if status == "incomplete" or incomplete_details is not None else "response_status"
        raise ProviderFailure(
            "INVALID_RESPONSE",
            _response_diagnostics(response, provider=provider, model=model, parser_stage=stage),
        )
    for item in getattr(response, "output", []) or []:
        for block in getattr(item, "content", []) or []:
            if getattr(block, "type", "") == "refusal":
                raise ProviderFailure(
                    "REFUSAL",
                    _response_diagnostics(response, provider=provider, model=model, parser_stage="refusal"),
                )
    output_text = _extract_output_text(response)
    if not output_text.strip():
        stage = "unsupported_response_shape" if getattr(response, "output", None) else "missing_text"
        raise ProviderFailure(
            "INVALID_RESPONSE",
            _response_diagnostics(response, provider=provider, model=model, parser_stage=stage),
        )
    try:
        parsed = json.loads(output_text)
    except (TypeError, ValueError) as exc:
        raise ProviderFailure(
            "INVALID_RESPONSE",
            _response_diagnostics(response, provider=provider, model=model, parser_stage="json_decode"),
        ) from exc
    usage = getattr(response, "usage", None)
    return {
        "data": parsed,
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        "diagnostics": _response_diagnostics(response, provider=provider, model=model, parser_stage=""),
    }
