"""Small deterministic guards for exact product-version research."""
from __future__ import annotations

import re
from typing import Any


_VERSION_PATTERNS = (
    ("eau_de_toilette", re.compile(r"\b(?:eau[\s_-]*de[\s_-]*toilette|edt)\b", re.I)),
    ("eau_de_parfum", re.compile(r"\b(?:eau[\s_-]*de[\s_-]*parfum|edp)\b", re.I)),
    ("elixir", re.compile(r"\belixir\b", re.I)),
    ("cologne", re.compile(r"\b(?:eau[\s_-]*de[\s_-]*cologne|cologne)\b", re.I)),
    ("body_mist", re.compile(r"\b(?:body|fragrance)[\s_-]*mist\b", re.I)),
    ("parfum", re.compile(r"\bparfum\b", re.I)),
)


def product_version_label(value: Any) -> str | None:
    """Return a controlled edition label when text identifies one explicitly."""
    text = str(value or "")
    # Specific multi-word editions must win over the generic word "parfum".
    for label, pattern in _VERSION_PATTERNS:
        if pattern.search(text):
            return label
    return None


def product_version_compatible(expected: Any, observed: Any) -> bool:
    """Reject only explicit edition conflicts; absence remains reviewable."""
    expected_label = product_version_label(expected)
    observed_label = product_version_label(observed)
    return not (expected_label and observed_label and expected_label != observed_label)

