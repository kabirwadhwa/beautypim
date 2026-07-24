import json
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass
class Classification:
    page_type: str
    score: int
    reasons: list[str]


def classify_page(url: str, html: str) -> Classification:
    soup = BeautifulSoup(html, "html.parser")
    reasons: list[str] = []
    score = 0
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or "{}")
            values = payload if isinstance(payload, list) else [payload]
            for value in values:
                graph = value.get("@graph", []) if isinstance(value, dict) else []
                candidates = [value, *graph]
                if any(str(item.get("@type", "")).lower() == "product" for item in candidates if isinstance(item, dict)):
                    score += 8
                    reasons.append("schema.org Product")
                    break
        except (json.JSONDecodeError, TypeError):
            continue
    text = soup.get_text(" ", strip=True).lower()
    signals = [
        ("price", 2, ("price", "€", "$", "£")),
        ("availability", 1, ("in stock", "out of stock", "availability")),
        ("basket control", 3, ("add to basket", "add to bag", "ajouter au panier")),
        ("ingredients", 2, ("ingredients", "ingrédients", "inci")),
    ]
    for label, weight, needles in signals:
        if any(needle in text for needle in needles):
            score += weight
            reasons.append(label)
    if soup.select_one("h1"):
        score += 1
        reasons.append("product title candidate")
    path = url.lower()
    if any(pattern in path for pattern in ("/product/", "/products/", "/p/")):
        score += 2
        reasons.append("product URL pattern")
    if score >= 7:
        return Classification("product", score, reasons)
    if soup.select("a[href*='page='], a[rel=next]"):
        return Classification("pagination", score, reasons + ["pagination links"])
    if any(word in path for word in ("/blog/", "/magazine/", "/conseils/")):
        return Classification("editorial", score, reasons + ["editorial URL pattern"])
    if len(soup.select("a[href*='/product'], a[href*='/p/']")) >= 2:
        return Classification("category", score, reasons + ["multiple product links"])
    return Classification("unknown", score, reasons)
