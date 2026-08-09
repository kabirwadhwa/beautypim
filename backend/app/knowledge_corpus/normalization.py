from __future__ import annotations

import html
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any


EMPTY = {"", "none", "nan", "null", "n/a", "-"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value)).encode("ascii", "ignore").decode()
    text = text.replace("'", "")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


BRAND_ALIASES = {
    "ysl": "yves saint laurent",
    "kiehls since 1851": "kiehls",
}


def normalized_brand(value: Any) -> str:
    brand = normalized_text(value)
    return BRAND_ALIASES.get(brand, brand)


def normalized_gtin(value: Any) -> str | None:
    text = clean_text(value)
    text = re.sub(r"\.0+$", "", text)
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) in {8, 12, 13, 14} else None


def normalized_identifier(value: Any) -> str | None:
    value = clean_text(value)
    return value if value and value.lower() not in EMPTY else None


def split_size(value: Any, explicit_unit: Any = None) -> tuple[str | None, str | None]:
    text = clean_text(value)
    unit = clean_text(explicit_unit).lower() or None
    if not text:
        return None, unit
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(fl\.?\s*oz|ml|cl|kg|mg|g|l|oz)\b", text, re.I)
    if not match:
        return text, unit
    return match.group(1).replace(",", "."), unit or re.sub(r"[.\s]+", "", match.group(2).lower())


def decimal_value(value: Any) -> Decimal | None:
    text = clean_text(value).replace("€", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def list_value(value: Any) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"\s*[|;]\s*|\s*,\s*(?=[A-ZÀ-Ý])", text) if part.strip()]


def stable_hash(*values: Any) -> str:
    return sha256("\x1f".join(clean_text(value) for value in values).encode("utf-8")).hexdigest()


CATEGORY_TRANSLATIONS = {
    "gezichtsverzorging": "Skin Care", "huidverzorging": "Skin Care",
    "haarverzorging": "Hair Care", "make up": "Makeup", "makeup": "Makeup",
    "parfum": "Fragrance", "geuren": "Fragrance", "lichaamsverzorging": "Body Care",
    "soins visage": "Skin Care", "soins capillaires": "Hair Care",
    "maquillage": "Makeup", "parfums": "Fragrance", "soins corps": "Body Care",
}


def normalized_category(value: Any) -> str | None:
    raw = clean_text(value)
    folded = normalized_text(raw)
    for source, target in CATEGORY_TRANSLATIONS.items():
        if normalized_text(source) in folded:
            return target
    return raw or None
