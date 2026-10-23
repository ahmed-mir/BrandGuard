"""Thin, lazy OpenAI Responses API adapter; importing this module makes no request."""
import json

from django.conf import settings

from .schemas import FINDING_SCHEMA, SYSTEM_INSTRUCTIONS


class ProviderFailure(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def request_analysis(payload):
    """Send one explicitly authorized request and return parsed JSON plus usage."""
    if not settings.BRANDGUARD_AI_ENABLED:
        raise ProviderFailure("AI_DISABLED")
    if not settings.OPENAI_API_KEY:
        raise ProviderFailure("MISSING_API_KEY")

    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        OpenAI,
        RateLimitError,
    )

    client = OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.BRANDGUARD_AI_TIMEOUT_SECONDS,
        max_retries=0,
    )
    try:
        response = client.responses.create(
            model=settings.BRANDGUARD_AI_MODEL,
            instructions=SYSTEM_INSTRUCTIONS,
            input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "brandguard_advisory_analysis",
                    "strict": True,
                    "schema": FINDING_SCHEMA,
                }
            },
            max_output_tokens=settings.BRANDGUARD_AI_MAX_OUTPUT_TOKENS,
            store=False,
        )
    except APITimeoutError as exc:
        raise ProviderFailure("TIMEOUT") from exc
    except AuthenticationError as exc:
        raise ProviderFailure("AUTHENTICATION") from exc
    except RateLimitError as exc:
        # Distinguish only the SDK's documented status class; never persist its raw body.
        code = getattr(exc, "code", None)
        raise ProviderFailure("QUOTA" if code == "insufficient_quota" else "PROVIDER_RATE_LIMIT") from exc
    except APIStatusError as exc:
        if exc.status_code == 429:
            code = getattr(exc, "code", None)
            raise ProviderFailure("QUOTA" if code == "insufficient_quota" else "PROVIDER_RATE_LIMIT") from exc
        raise ProviderFailure("PROVIDER_UNAVAILABLE") from exc
    except APIConnectionError as exc:
        raise ProviderFailure("PROVIDER_UNAVAILABLE") from exc

    if getattr(response, "status", "completed") != "completed":
        raise ProviderFailure("INVALID_RESPONSE")
    for item in getattr(response, "output", []) or []:
        for block in getattr(item, "content", []) or []:
            if getattr(block, "type", "") == "refusal":
                raise ProviderFailure("REFUSAL")
    output_text = getattr(response, "output_text", "") or ""
    if not output_text.strip():
        raise ProviderFailure("INVALID_RESPONSE")
    try:
        parsed = json.loads(output_text)
    except (TypeError, ValueError) as exc:
        raise ProviderFailure("INVALID_RESPONSE") from exc
    usage = getattr(response, "usage", None)
    return {
        "data": parsed,
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
    }
