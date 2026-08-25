"""Railway-visible structured logging for Improve Product research jobs."""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit, urlunsplit

PREFIX = "[PRODUCT_RESEARCH]"
LOGGER_NAME = "app.product_research"
_SENSITIVE_KEYS = (
    "api_key", "authorization", "credential", "password", "secret", "token", "headers",
    "review_text", "review_body", "review_content", "reviewer", "customer",
)


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        pass
    return value


def _sanitize(value, key: str = ""):
    lowered = key.lower()
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
