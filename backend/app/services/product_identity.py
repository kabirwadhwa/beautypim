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


def trusted_product_version(db, product) -> str | None:
    """Return a fragrance edition only when source data or a human supplied it."""
    from app.models import FieldValue, SourceListing

    rows = db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name.in_(["product_type", "subcategory"]),
        FieldValue.is_current == True,
        FieldValue.source_type.in_(["source_data", "human_edit"]),
    ).all()
    for row in rows:
        label = product_version_label(row.value)
        if label:
            return label
    if product_version_label(product.product_name):
        return product_version_label(product.product_name)
    listings = db.query(SourceListing.raw_data).filter(
        SourceListing.canonical_product_id == product.id,
    ).order_by(SourceListing.created_at.desc()).limit(10).all()
    for (raw_data,) in listings:
        for value in (raw_data or {}).values():
            label = product_version_label(value)
            if label:
                return label
    return None


def product_is_fragrance(db, product) -> bool:
    """Detect fragrance scope without treating a concentration guess as truth."""
    from app.models import Category, FieldValue, SourceListing

    values = [product.product_name]
    values.extend(row.value for row in db.query(FieldValue).filter(
        FieldValue.canonical_product_id == product.id,
        FieldValue.field_name.in_(["product_type", "subcategory"]),
        FieldValue.is_current == True,
    ).all())
    if product.category_id:
        category = db.query(Category).filter(Category.id == product.category_id).first()
        if category:
            values.append(category.path)
    listing = db.query(SourceListing.raw_data).filter(
        SourceListing.canonical_product_id == product.id,
    ).order_by(SourceListing.created_at.desc()).first()
    if listing:
        values.extend((listing[0] or {}).values())
    text = " ".join(str(value or "") for value in values).lower()
    return any(token in text for token in ("perfume", "parfum", "fragrance", "eau de", "cologne"))
