from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

from app.scraping import PARSER_VERSION
from app.scraping.adapters.base import ProductAdapter
from app.scraping.schemas import ExtractedField, ScrapedProduct


def nested(record: dict, *keys):
    value: Any = record
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def label(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("label") or value.get("description") or value.get("name")
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def labels(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values else []
    result = []
    for value in values:
        item = label(value)
        if item and item not in result:
            result.append(item)
    return result


def sanitize_html(value: Any) -> Optional[str]:
    if value is None:
        return None
    soup = BeautifulSoup(str(value), "html.parser")
    for unsafe in soup(["script", "style", "template"]):
        unsafe.decompose()
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in soup.get_text("\n", strip=True).splitlines()
    ]
    clean = "\n".join(line for line in lines if line)
    return clean or None


def decimal_value(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def date_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("$date")
    return str(value).strip() if value else None


def normalized_gtin(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) in {8, 12, 13, 14} else None


def image_urls(record: dict) -> list[str]:
    found = []
    pictures = nested(record, "assets", "pictures") or []
    for picture in pictures if isinstance(pictures, list) else []:
        candidates = []
        for exportable in picture.get("exportables", []) if isinstance(picture, dict) else []:
            uri = exportable.get("uniformResourceIdentifier")
            if uri:
                candidates.append((int(exportable.get("width") or 0), uri))
        for key in ("url", "masterUniformResourceIdentifier"):
            if isinstance(picture, dict) and picture.get(key):
                candidates.append((int(picture.get("width") or 0), picture[key]))
        if candidates:
            uri = max(candidates, key=lambda item: item[0])[1]
            if uri not in found:
                found.append(uri)
    return found


class Retail DataDatasetAdapter(ProductAdapter):
    name = "retail_data_active_products_export"
    version = "1.0.0"

    def parse(self, html: str, url: str, *, country=None, locale=None):
        raise NotImplementedError("Use parse_record for structured Retail Data exports")

    def parse_record(self, record: dict, imported_at: datetime) -> ScrapedProduct:
        retek = nested(record, "retekEnrichment", "masterData") or {}
        record_id = str(record.get("_id") or "").strip()
        retailer_product_id = str(
            retek.get("productId") or record.get("id") or record_id
        ).strip()
        base_product_id = str(retek.get("baseProductId") or retailer_product_id).strip()
        source_url = f"https://retail-data.invalid/p/{base_product_id}"
        brand = label(record.get("retail_dataBrandCode")) or label(record.get("brand"))
        product_name = label(nested(record, "naming", "longName"))
        composition = record.get("composition") or []
        if not isinstance(composition, list):
            composition = [composition]
        composition = [str(item).strip() for item in composition if str(item).strip()]
        raw_inci = ", ".join(composition) or None
        category = record.get("isClassifiedIn") or {}
        category_path = [
            item for item in (
                label(value) for value in category.get("path", [])
            ) if item
        ]
        leaf = label(category.get("name"))
        if leaf and (not category_path or category_path[-1] != leaf):
            category_path.append(leaf)
        size_value = nested(record, "netSizing", "content", "value")
        unit = label(nested(record, "netSizing", "content", "unityLabel")) or label(
            nested(record, "netSizing", "content", "unity")
        )
        retail_price = decimal_value(
            nested(record, "retailPriceIncludingVAT", "organisation1337", "value")
        )
        currency = label(
            nested(record, "retailPriceIncludingVAT", "organisation1337", "currency")
        )
        availability = label(nested(record, "retekEnrichment", "masterData", "erp", "status"))
        benefits = labels(record.get("productBenefits"))
        warnings = labels(record.get("warnings"))
        advice = "\n".join(
            value for value in (
                sanitize_html(item) for item in (record.get("advices") or [])
            ) if value
        ) or None
        product = ScrapedProduct(
            source_name="Retail Data active products export",
            source_domain="retail-data.invalid",
            source_url=source_url,
            canonical_url=source_url,
            scraped_at=imported_at,
            locale="fr-FR",
            country="FR",
            retailer_product_id=retailer_product_id,
            brand=brand,
            product_name=product_name,
            subtitle=label(nested(record, "naming", "shortName")),
            description=sanitize_html(record.get("description")),
            gtin=(
                normalized_gtin(record.get("barcodeScanText"))
                or normalized_gtin(retek.get("ean"))
                or normalized_gtin(record_id)
            ),
            sku=retailer_product_id,
            category_path=category_path,
            product_type=leaf or label(record.get("axisOrganization1337")),
            variant_name=label(record.get("colorDescription")) or (
                f"{size_value} {unit}" if size_value is not None and unit else None
            ),
            size=str(size_value) if size_value is not None else None,
            unit=unit,
            shade=label(record.get("colorDescription")),
            price=retail_price,
            currency=currency or "EUR",
            availability=availability,
            image_urls=image_urls(record),
            ingredient_text_raw=raw_inci,
            ingredients=composition,
            claims=labels(record.get("isTaggedBy")),
            benefits=benefits,
            usage_instructions=advice,
            warnings=warnings,
            skin_types=labels(record.get("skinTypeList")),
            parser_version=PARSER_VERSION,
        )
        field_paths = {
            "brand": "retail_dataBrandCode.label",
            "product_name": "naming.longName",
            "subtitle": "naming.shortName",
            "description": "description (HTML sanitized)",
            "gtin": "barcodeScanText|retekEnrichment.masterData.ean|_id",
            "sku": "retekEnrichment.masterData.productId",
            "category_path": "isClassifiedIn.path|isClassifiedIn.name",
            "product_type": "isClassifiedIn.name",
            "variant_name": "colorDescription|netSizing.content",
            "size": "netSizing.content.value",
            "unit": "netSizing.content.unity",
            "shade": "colorDescription",
            "price": "retailPriceIncludingVAT.organisation1337.value",
            "currency": "retailPriceIncludingVAT.organisation1337.currency",
            "availability": "retekEnrichment.masterData.erp.status",
            "image_urls": "assets.pictures[].exportables[].uniformResourceIdentifier",
            "ingredient_text_raw": "composition[]",
            "ingredients": "composition[]",
            "claims": "isTaggedBy[]",
            "benefits": "productBenefits[]",
            "usage_instructions": "advices[]",
            "warnings": "warnings[]",
            "skin_types": "skinTypeList[]",
        }
        raw_values = {
            "brand": record.get("retail_dataBrandCode"),
            "product_name": nested(record, "naming", "longName"),
            "subtitle": nested(record, "naming", "shortName"),
            "description": record.get("description"),
            "gtin": {
                "barcodeScanText": record.get("barcodeScanText"),
                "retekEan": retek.get("ean"),
                "_id": record_id,
            },
            "sku": retek.get("productId"),
            "category_path": record.get("isClassifiedIn"),
            "product_type": record.get("isClassifiedIn"),
            "variant_name": {
                "colorDescription": record.get("colorDescription"),
                "content": nested(record, "netSizing", "content"),
            },
            "size": nested(record, "netSizing", "content"),
            "unit": nested(record, "netSizing", "content"),
            "shade": record.get("colorDescription"),
            "price": nested(record, "retailPriceIncludingVAT", "organisation1337"),
            "currency": nested(record, "retailPriceIncludingVAT", "organisation1337"),
            "availability": nested(record, "retekEnrichment", "masterData", "erp"),
            "image_urls": nested(record, "assets", "pictures"),
            "ingredient_text_raw": record.get("composition"),
            "ingredients": record.get("composition"),
            "claims": record.get("isTaggedBy"),
            "benefits": record.get("productBenefits"),
            "usage_instructions": record.get("advices"),
            "warnings": record.get("warnings"),
            "skin_types": record.get("skinTypeList"),
        }
        for field_name, path in field_paths.items():
            value = getattr(product, field_name)
            if value not in (None, "", []):
                product.fields[field_name] = ExtractedField(
                    value=value,
                    raw_value=raw_values.get(field_name, value),
                    path=path,
                    method="retail_data_structured_export",
                )
        extra_fields = {
            "source_record_id": record_id,
            "source_system": record.get("source"),
            "source_created_at": date_value(record.get("creationDate")),
            "source_last_updated_at": date_value(record.get("lastUpdateDate")),
            "origin_country": label(record.get("originCountry")),
            "target_market": label(record.get("targetMarket")),
            "target_gender": label(record.get("targetConsumerGender")),
            "texture": labels(record.get("productTextureList")),
            "area_of_use": labels(record.get("areaOfUseList")),
            "fragrance_head_notes": labels(record.get("headNote")),
            "fragrance_heart_notes": labels(record.get("heartNote")),
            "fragrance_base_notes": labels(record.get("baseNote")),
            "refillable": label(nested(record, "refillableAttribute", "type")),
            "catalog_price": record.get("catalogPrice"),
        }
        for field_name, value in extra_fields.items():
            if value not in (None, "", []):
                product.fields[field_name] = ExtractedField(
                    value=value,
                    raw_value=value,
                    path=field_name,
                    method="retail_data_structured_export",
                )
        return product
