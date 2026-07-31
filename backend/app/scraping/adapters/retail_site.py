import json
import re

from bs4 import BeautifulSoup

from app.scraping.adapters.generic import GenericJsonLdAdapter
from app.scraping.schemas import ExtractedField
from app.scraping.ingredients import split_inci


class RetailSiteAdapter(GenericJsonLdAdapter):
    name = "retail_site"
    version = "1.0.0"

    def parse(self, html: str, url: str, *, country=None, locale=None):
        product = super().parse(html, url, country=country or "FR", locale=locale or "fr-FR")
        if not product:
            return None
        soup = BeautifulSoup(html, "html.parser")
        selectors = {
            "ingredient_text_raw": [
                "[data-testid*=ingredient]", ".product-ingredients", "#ingredients",
                "[class*=ingredients]",
            ],
            "usage_instructions": [
                "[data-testid*=advice]", ".product-advice", "[class*=conseil]",
            ],
        }
        for field, candidates in selectors.items():
            for selector in candidates:
                node = soup.select_one(selector)
                if node:
                    value = node.get_text(" ", strip=True)
                    if value:
                        setattr(product, field, value)
                        product.fields[field] = ExtractedField(
                            value=value, raw_value=value, path=selector,
                            method="retail_site_selector",
                        )
                        break
        for script in soup.select("script"):
            text = script.string or ""
            if "__NEXT_DATA__" not in (script.get("id") or "") and "ingredients" not in text.lower():
                continue
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            match = re.search(r'"ingredients?"\s*:\s*"([^"]+)"', json.dumps(payload))
            if match and not product.ingredient_text_raw:
                product.ingredient_text_raw = match.group(1)
                product.fields["ingredient_text_raw"] = ExtractedField(
                    value=match.group(1), raw_value=match.group(1),
                    path="embedded_application_state.ingredients",
                    method="embedded_json",
                )
        product.ingredients = split_inci(product.ingredient_text_raw)
        return product
