"""Model-aware cost estimation for every paid AI call.

Cost is an *estimate*, and this module is explicit about where each estimate
comes from so the interface can say so too. Three sources, in precedence order:

1. ``override``   - operator-supplied rates from ``AI_MODEL_PRICING_JSON``.
                    Authoritative; use this to correct a rate without a deploy.
2. ``reference``  - the seeded rate table below. A point-in-time snapshot of
                    published list prices, not a live feed. Provider prices
                    change, so treat these as a starting default and override
                    them for anything billing-critical.
3. ``calibrated`` - derived from this deployment's own historical runs: the
                    observed blended dollars-per-token of past calls for the
                    same model. Used when a model has no configured rate, which
                    is what keeps newly swapped-in models from reporting $0.

If none of the three yields a rate, the estimate is ``None`` and the token
counts still stand on their own. A missing price never fabricates a zero.

Rates are USD per 1,000,000 tokens.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Snapshot of published list prices, USD per 1M tokens. `cached_input` applies
# to prompt tokens the provider reports as cache hits, which bill lower.
#
# The gpt-4o-mini and gemini-1.5-flash entries intentionally reproduce the
# hardcoded rates this module replaced, so historical figures stay comparable.
# Entries marked needs_confirmation are our best available estimate for a model
# whose rate was never encoded anywhere in this codebase; confirm against your
# own invoice and set AI_MODEL_PRICING_JSON rather than trusting them.
REFERENCE_PRICING: dict[str, dict[str, Any]] = {
    # OpenAI
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cached_input": 0.075},
    "gpt-4o": {"input": 2.50, "output": 10.00, "cached_input": 1.25},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60, "cached_input": 0.10},
    "gpt-4.1": {"input": 2.00, "output": 8.00, "cached_input": 0.50},
    "gpt-5-mini": {"input": 0.25, "output": 2.00, "cached_input": 0.025,
                   "needs_confirmation": True},
    "gpt-5": {"input": 1.25, "output": 10.00, "cached_input": 0.125,
              "needs_confirmation": True},
    "gpt-5.6": {"input": 1.25, "output": 10.00, "cached_input": 0.125,
                "needs_confirmation": True},
    # Google
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50,
                         "needs_confirmation": True},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00,
                       "needs_confirmation": True},
}

# A web search billed as a tool call, separate from token cost. OpenAI's
# Responses API reports searches in `usage`; price per 1k calls.
DEFAULT_WEB_SEARCH_CALL_USD = 0.01

_CENT = Decimal("0.000001")


@dataclass(frozen=True)
class ModelPricing:
    """Resolved rates for one model, with the provenance of the figures."""

    model: str
    input_per_million: Optional[float]
    output_per_million: Optional[float]
    cached_input_per_million: Optional[float]
    source: str  # override | reference | calibrated | unknown
    needs_confirmation: bool = False

    @property
    def is_priced(self) -> bool:
        return self.input_per_million is not None and self.output_per_million is not None


_UNKNOWN = ModelPricing(
    model="", input_per_million=None, output_per_million=None,
    cached_input_per_million=None, source="unknown",
)

_override_cache: dict[str, dict[str, Any]] | None = None
_override_lock = threading.Lock()

# Calibrated rates are derived from the database, so they are cached per model
# rather than recomputed on every call.
_calibration_cache: dict[str, ModelPricing] = {}
_calibration_lock = threading.Lock()


def _normalize(model: str) -> str:
    return str(model or "").strip().lower()


def load_pricing_overrides() -> dict[str, dict[str, Any]]:
    """Parse AI_MODEL_PRICING_JSON once, tolerating a malformed value."""
    global _override_cache
    if _override_cache is not None:
        return _override_cache
    with _override_lock:
        if _override_cache is not None:
            return _override_cache
        raw = getattr(settings, "AI_MODEL_PRICING_JSON", None)
        parsed: dict[str, dict[str, Any]] = {}
        if raw:
            try:
                candidate = json.loads(raw)
                if not isinstance(candidate, dict):
                    raise ValueError("pricing override must be a JSON object")
                for model, rates in candidate.items():
                    if not isinstance(rates, dict):
                        continue
                    entry: dict[str, Any] = {}
                    for key in ("input", "output", "cached_input"):
                        if isinstance(rates.get(key), (int, float)):
                            entry[key] = float(rates[key])
                    if "input" in entry and "output" in entry:
                        parsed[_normalize(model)] = entry
            except (ValueError, TypeError) as exc:
                # A bad override must not take the application down; fall back
                # to reference pricing and make the misconfiguration loud.
                logger.error(
                    "AI_MODEL_PRICING_JSON could not be parsed and was ignored: %s", exc
                )
                parsed = {}
        _override_cache = parsed
        return _override_cache


def reset_pricing_caches() -> None:
    """Clear memoized pricing. For tests and for a live config reload."""
    global _override_cache
    with _override_lock:
        _override_cache = None
    with _calibration_lock:
        _calibration_cache.clear()


def _match_table(model: str, table: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Exact match first, then the longest prefix.

    Providers append dated suffixes to model ids (``gpt-4o-mini-2024-07-18``),
    so a prefix match keeps a pinned snapshot priced like its family. Longest
    wins so ``gpt-5-mini`` never resolves against ``gpt-5``.
    """
    key = _normalize(model)
    if not key:
        return None
    if key in table:
        return table[key]
    candidates = [name for name in table if key.startswith(name)]
    if not candidates:
        return None
    return table[max(candidates, key=len)]


def resolve_pricing(model: str, db: Any = None) -> ModelPricing:
    """Resolve rates for a model: override, then reference, then calibration."""
    key = _normalize(model)
    if not key:
        return _UNKNOWN

    override = _match_table(key, load_pricing_overrides())
    if override:
        return ModelPricing(
            model=key,
            input_per_million=override["input"],
            output_per_million=override["output"],
            cached_input_per_million=override.get("cached_input"),
            source="override",
        )

    reference = _match_table(key, REFERENCE_PRICING)
    if reference:
        return ModelPricing(
            model=key,
            input_per_million=reference["input"],
            output_per_million=reference["output"],
            cached_input_per_million=reference.get("cached_input"),
            source="reference",
            needs_confirmation=bool(reference.get("needs_confirmation")),
        )

    if db is not None:
        calibrated = calibrate_from_history(db, key)
        if calibrated.is_priced:
            return calibrated

    return _UNKNOWN


def calibrate_from_history(db: Any, model: str) -> ModelPricing:
    """Estimate a model's rate from what past calls actually cost.

    Historical ``EnrichmentRun`` rows carry both token counts and the cost that
    was recorded at the time. Their ratio gives a blended dollars-per-token for
    this deployment's real traffic mix. It cannot separate input from output
    rates, so the blended figure is applied to both; for an unpriced model that
    is a far better answer than reporting nothing.
    """
    key = _normalize(model)
    with _calibration_lock:
        if key in _calibration_cache:
            return _calibration_cache[key]

    result = _UNKNOWN
    try:
        from sqlalchemy import func as sa_func

        from app.models import EnrichmentRun

        row = (
            db.query(
                sa_func.sum(EnrichmentRun.prompt_tokens),
                sa_func.sum(EnrichmentRun.completion_tokens),
                sa_func.sum(EnrichmentRun.estimated_cost),
            )
            .filter(
                EnrichmentRun.model == model,
                EnrichmentRun.estimated_cost.isnot(None),
                EnrichmentRun.prompt_tokens.isnot(None),
                EnrichmentRun.completion_tokens.isnot(None),
            )
            .first()
        )
        prompt_total = int(row[0] or 0) if row else 0
        completion_total = int(row[1] or 0) if row else 0
        cost_total = float(row[2] or 0.0) if row else 0.0
        token_total = prompt_total + completion_total
        if token_total > 0 and cost_total > 0:
            per_million = (cost_total / token_total) * 1_000_000
            result = ModelPricing(
                model=key,
                input_per_million=per_million,
                output_per_million=per_million,
                cached_input_per_million=None,
                source="calibrated",
            )
    except Exception as exc:  # pragma: no cover - calibration is best-effort
        logger.warning("Pricing calibration for %s failed: %s", model, exc)

    with _calibration_lock:
        _calibration_cache[key] = result
    return result


def estimate_cost(
    model: str,
    prompt_tokens: Optional[int] = 0,
    completion_tokens: Optional[int] = 0,
    *,
    cached_prompt_tokens: Optional[int] = 0,
    reasoning_tokens: Optional[int] = 0,
    web_search_calls: Optional[int] = 0,
    db: Any = None,
) -> tuple[Optional[Decimal], str]:
    """Estimate one call's cost. Returns ``(usd_or_none, pricing_source)``.

    ``reasoning_tokens`` covers provider-side thinking tokens, which bill at the
    output rate and are reported outside the visible completion. Gemini 2.5
    reports them as ``thoughtsTokenCount`` and omits them from
    ``candidatesTokenCount``, so leaving them out understates the bill.

    ``cached_prompt_tokens`` is a subset of ``prompt_tokens``; the cached
    portion is charged at the cached rate and the remainder at the full rate.
    """
    pricing = resolve_pricing(model, db=db)
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cached = min(max(0, int(cached_prompt_tokens or 0)), prompt)
    reasoning = max(0, int(reasoning_tokens or 0))
    searches = max(0, int(web_search_calls or 0))

    if not pricing.is_priced:
        return None, pricing.source

    uncached = prompt - cached
    cached_rate = (
        pricing.cached_input_per_million
        if pricing.cached_input_per_million is not None
        else pricing.input_per_million
    )

    total = (
        Decimal(str(uncached)) * Decimal(str(pricing.input_per_million))
        + Decimal(str(cached)) * Decimal(str(cached_rate))
        + Decimal(str(completion + reasoning)) * Decimal(str(pricing.output_per_million))
    ) / Decimal(1_000_000)

    if searches:
        rate = getattr(settings, "AI_WEB_SEARCH_CALL_USD", DEFAULT_WEB_SEARCH_CALL_USD)
        total += Decimal(str(searches)) * Decimal(str(rate))

    return total.quantize(_CENT, rounding=ROUND_HALF_UP), pricing.source


def normalize_openai_usage(usage: Any) -> dict[str, int]:
    """Read token counts from either OpenAI usage shape.

    Chat Completions reports ``prompt_tokens``/``completion_tokens``; the
    Responses API reports ``input_tokens``/``output_tokens``. Both are in play
    here, so accept either rather than silently reading zero.
    """
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")

    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else 0

    out_details = (
        usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    )
    reasoning = out_details.get("reasoning_tokens") if isinstance(out_details, dict) else 0

    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "cached_prompt_tokens": int(cached or 0),
        "reasoning_tokens": int(reasoning or 0),
    }


def normalize_gemini_usage(usage_metadata: Any) -> dict[str, int]:
    """Read token counts from Gemini ``usageMetadata``.

    ``thoughtsTokenCount`` is billed but excluded from ``candidatesTokenCount``,
    so it is carried separately as reasoning tokens.
    """
    usage = usage_metadata if isinstance(usage_metadata, dict) else {}
    return {
        "prompt_tokens": int(usage.get("promptTokenCount") or 0),
        "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
        "cached_prompt_tokens": int(usage.get("cachedContentTokenCount") or 0),
        "reasoning_tokens": int(usage.get("thoughtsTokenCount") or 0),
    }


def pricing_disclosure(model: str, db: Any = None) -> dict[str, Any]:
    """Describe how a model's cost is being estimated, for display."""
    pricing = resolve_pricing(model, db=db)
    labels = {
        "override": "Configured rate",
        "reference": "Published reference rate",
        "calibrated": "Estimated from this deployment's historical runs",
        "unknown": "No rate available; cost not estimated",
    }
    return {
        "model": model,
        "pricing_source": pricing.source,
        "pricing_source_label": labels[pricing.source],
        "input_per_million_usd": pricing.input_per_million,
        "output_per_million_usd": pricing.output_per_million,
        "needs_confirmation": pricing.needs_confirmation,
        "is_estimate": True,
    }
