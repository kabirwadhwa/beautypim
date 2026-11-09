"""Railway-visible structured logging for Improve Product research jobs."""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit, urlunsplit

PREFIX = "[PRODUCT_RESEARCH]"
LOGGER_NAME = "app.product_research"
_SENSITIVE_KEYS = (
    "api_key", "authorization", "credential", "password", "secret", "headers",
    "access_token", "refresh_token", "bearer_token", "auth_token", "id_token",
    "token_value", "api_token",
    "review_text", "review_body", "review_content", "reviewer", "customer",
)
# A bare "token" substring used to appear above, which redacted every token
# *count* as well - input_tokens, output_tokens and total_tokens all became
# [REDACTED], destroying the only record of what a research job consumed. The
# credential-bearing keys are now matched by their full names instead, and
# these count keys are allowed through explicitly so a future broad pattern
# cannot silently swallow them again.
_TOKEN_COUNT_KEYS = frozenset({
    "tokens", "total_tokens", "input_tokens", "output_tokens",
    "prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens",
    "cached_prompt_tokens", "input_tokens_details", "output_tokens_details",
    "prompt_tokens_details", "completion_tokens_details",
    "token_count", "prompt_token_count", "candidates_token_count",
    "thoughts_token_count", "total_token_count",
})


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        pass
    return value


def _sanitize_value_only(value):
    """Keep a token-count structure intact without re-testing its key names."""
    if isinstance(value, dict):
        return {str(k): _sanitize_value_only(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value_only(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:120]


def _sanitize(value, key: str = ""):
    lowered = key.lower()
    if lowered in _TOKEN_COUNT_KEYS:
        return _sanitize_value_only(value)
    if any(part in lowered for part in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        value = _safe_url(value)
        return value if len(value) <= 500 else value[:497] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def product_research_log(event: str, *, level: int = logging.INFO, **payload) -> None:
    """Emit one concise, redacted JSON record to the production process stream."""
    logger = logging.getLogger(LOGGER_NAME)
    # Explicitly keep this observability channel at INFO even when a library
    # logger has a more restrictive inherited level. It still propagates to
    # the application's stdout JSON handler configured in app.main.
    logger.setLevel(logging.INFO)
    record = {"event": event, **payload}
    logger.log(level, "%s %s", PREFIX, json.dumps(_sanitize(record), separators=(",", ":"), ensure_ascii=False))
