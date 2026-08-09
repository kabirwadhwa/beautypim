from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable


@dataclass
class CorpusRecord:
    dataset_key: str
    sheet: str
    row_number: int
    raw_payload: dict[str, Any]
    source_record_id: str | None
    source_parent_id: str | None
    brand: str
    product_name: str
    variant_name: str | None = None
    gtin: str | None = None
    size_value: str | None = None
    size_unit: str | None = None
    shade: str | None = None
    colour: str | None = None
    undertone: str | None = None
    category: str | None = None
    subcategory: str | None = None
    product_type: str | None = None
    application_area: str | None = None
    locale: str | None = None
    market: str | None = None
    source_retailer: str | None = None
    source_url: str | None = None
    observed_at: datetime | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    raw_inci: str | None = None
    inci_language: str | None = None
    price: Decimal | None = None
    original_price: Decimal | None = None
    currency: str | None = None
    availability: str | None = None
    image_url: str | None = None
    skip_reason: str | None = None


class CorpusAdapter:
    name = "base"
    version = "1.0"

    def inspect(self, path: str) -> dict[str, Any]:
        raise NotImplementedError

    def iter_records(self, path: str, *, start_row: int = 0, limit: int | None = None) -> Iterable[CorpusRecord]:
        raise NotImplementedError
