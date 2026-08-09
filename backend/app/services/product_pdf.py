"""Compact, category-aware BeautyPIM product-sheet PDF renderer."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from html import escape
from io import BytesIO
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.image_urls import fetch_public_image


NAVY = colors.HexColor("#001D55")
INK = colors.HexColor("#07142D")
MUTED = colors.HexColor("#5D6470")
LINE = colors.HexColor("#CAD0D9")
PALE = colors.HexColor("#F4F6F9")
WHITE = colors.white

PAGE_W, PAGE_H = A4
MARGIN = 7 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
@dataclass(frozen=True)
class DensityPreset:
    name: str
    brand: float
    product: float
    meta: float
    positioning: float
    body: float
    label: float
    section: float
    ingredient: float
    number: float
    footer: float
    image_width: float
    image_height: float
    gap: float
    card_pad_x: float
    card_pad_y: float
    header_pad_y: float


DENSITY_PRESETS = {
    "low": DensityPreset(
        "low", 17, 28, 12.5, 12.2, 12.2, 11.5, 12.3, 10.8, 14, 8,
        50 * mm, 50 * mm, 5 * mm, 9, 10, 7,
    ),
    "medium": DensityPreset(
        "medium", 16, 25, 12, 11.5, 11.2, 10.6, 11.4, 9.8, 12.8, 8,
        45 * mm, 45 * mm, 3.5 * mm, 8, 7, 5.5,
    ),
    "high": DensityPreset(
        "high", 15, 22, 10.5, 10.2, 9.8, 9.4, 10.2, 8.8, 11.2, 7.7,
        40 * mm, 40 * mm, 2.2 * mm, 6, 5, 4,
    ),
}

_DENSITY: ContextVar[DensityPreset] = ContextVar("pdf_density", default=DENSITY_PRESETS["medium"])


def _style(name: str, size: float, *, bold: bool = False, color=INK,
           leading: float | None = None, align: int = 0) -> ParagraphStyle:
    return ParagraphStyle(
        name,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size,
        leading=leading or size * 1.22,
        textColor=color,
        alignment=align,
        spaceBefore=0,
        spaceAfter=0,
    )


def _styles() -> dict[str, ParagraphStyle]:
    density = _DENSITY.get()
    return {
        "brand": _style("brand", density.brand, bold=True, color=NAVY, leading=density.brand * 1.08),
        "product": _style("product", density.product, bold=True, color=NAVY, leading=density.product * 1.05),
        "meta": _style("meta", density.meta, bold=True, color=MUTED, leading=density.meta * 1.18),
        "positioning": _style("positioning", density.positioning, leading=density.positioning * 1.28),
        "body": _style("body", density.body, leading=density.body * 1.3),
        "body_bold": _style("body_bold", density.body, bold=True, leading=density.body * 1.3),
        "small": _style("small", density.ingredient, leading=density.ingredient * 1.24),
        "label": _style("label", density.label, bold=True, color=NAVY, leading=density.label * 1.25),
        "section": _style("section", density.section, bold=True, color=WHITE, leading=density.section * 1.12),
        "number": _style("number", density.number, bold=True, color=NAVY, leading=density.number * 1.1, align=TA_CENTER),
        "placeholder": _style("placeholder", density.ingredient, color=MUTED, leading=density.ingredient * 1.22),
        "footer": _style("footer", density.footer, color=MUTED, leading=density.footer * 1.15, align=TA_CENTER),
    }


def _clean(value: Any, fallback: str = "") -> str:
    if value is None or value == "" or value == []:
        return fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return _clean(
            value.get("text") or value.get("statement") or value.get("value")
            or value.get("review_message") or value.get("values"), fallback,
        )
    if isinstance(value, list):
        return ", ".join(filter(None, (_clean(item) for item in value))) or fallback
    result = str(value).strip()
    return fallback if result.lower() in {
        "", "unknown", "none", "null", "nan", "not provided", "not enriched",
    } else result


def _items(value: Any) -> list[str]:
    source = value if isinstance(value, list) else [value] if value else []
    return [text for text in (_clean(item) for item in source) if text]


def _p(value: Any, style: str = "body", fallback: str = "") -> Paragraph:
    text = escape(_clean(value, fallback)).replace("\n", "<br/>")
    return Paragraph(text, _styles()[style])


def _current_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        item["field_name"]: item.get("value")
        for item in data.get("field_values", [])
        if item.get("is_current") and item.get("field_name")
    }


def _review_lines(data: dict[str, Any]) -> list[str]:
    for observation in data.get("market_observations") or []:
        if not isinstance(observation, dict):
            continue
        rating = observation.get("rating")
        count = observation.get("review_count")
        summary = observation.get("review_summary")
        if rating in (None, "") and count in (None, "") and summary in (None, "", {}, []):
            continue
        lines: list[str] = []
        headline: list[str] = []
        if rating not in (None, ""):
            headline.append(f"{_clean(rating)}/5")
        if count not in (None, ""):
            headline.append(f"{_clean(count)} reviews")
        if headline:
            lines.append("  |  ".join(headline))
        if isinstance(summary, dict):
            summary = (summary.get("summary") or summary.get("text")
                       or summary.get("review_summary") or summary.get("highlights")
                       or summary.get("sentiment"))
        if _clean(summary):
            lines.append(_clean(summary))
        source = _clean(observation.get("source_name") or observation.get("source_domain"))
        if source:
            lines.append(f"Source: {source}")
        return lines
    return []


def _claims(current: dict[str, Any]) -> list[str]:
    return [
        _clean(item.get("name"))
        for item in (current.get("claims") or [])
        if isinstance(item, dict)
        and item.get("status") in {"verified", "source_supported"}
        and str(item.get("value", "")).lower() not in {"no", "false"}
        and _clean(item.get("name"))
    ]


def _bullet_table(values: Iterable[str], width: float, *, numbered: bool = False,
                  placeholder: str | None = None) -> Table:
    items = [item for item in values if _clean(item)]
    if not items and placeholder:
        return Table([[_p(placeholder, "placeholder")]], colWidths=[width], style=TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
    rows = []
    for index, item in enumerate(items, 1):
        marker = f"{index:02d}" if numbered else "✓"
        rows.append([_p(marker, "number"), _p(item)])
    return Table(rows, colWidths=[9 * mm, width - 9 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))


def _label_table(rows: Iterable[tuple[str, Any]], width: float,
                 label_width: float = 31 * mm) -> Table:
    clean_rows = [(label, _clean(value)) for label, value in rows if _clean(value)]
    if not clean_rows:
        return Table([[_p("No applicable profile details are available.", "placeholder")]],
                     colWidths=[width], style=TableStyle([
                         ("LEFTPADDING", (0, 0), (-1, -1), 0),
                         ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                         ("TOPPADDING", (0, 0), (-1, -1), 0),
                         ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                     ]))
    return Table(
        [[_p(label, "label"), _p(value)] for label, value in clean_rows],
        colWidths=[min(label_width, width * .42), width - min(label_width, width * .42)],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]),
    )


def _card(title: str, content: Any, width: float, *, pale: bool = False) -> Table:
    density = _DENSITY.get()
    return Table(
        [[_p(title.upper(), "section")], [content]],
        colWidths=[width],
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("BACKGROUND", (0, 1), (-1, 1), PALE if pale else WHITE),
            ("BOX", (0, 0), (-1, -1), .45, LINE),
            ("LEFTPADDING", (0, 0), (-1, 0), density.card_pad_x),
            ("RIGHTPADDING", (0, 0), (-1, 0), density.card_pad_x),
            ("TOPPADDING", (0, 0), (-1, 0), density.header_pad_y),
            ("BOTTOMPADDING", (0, 0), (-1, 0), density.header_pad_y),
            ("LEFTPADDING", (0, 1), (-1, 1), density.card_pad_x),
            ("RIGHTPADDING", (0, 1), (-1, 1), density.card_pad_x),
            ("TOPPADDING", (0, 1), (-1, 1), density.card_pad_y),
            ("BOTTOMPADDING", (0, 1), (-1, 1), density.card_pad_y),
        ]),
    )


def _row(cards: list[Any], widths: list[float]) -> Table:
    return Table([cards], colWidths=widths, hAlign="LEFT", style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", _DENSITY.get().footer)
    canvas.drawString(MARGIN, 4.3 * mm, "BeautyPIM product intelligence")
    canvas.drawRightString(PAGE_W - MARGIN, 4.3 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _document(title: str, story: list[Any]) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=6 * mm,
        bottomMargin=8 * mm,
        title=title,
        author="Beauty PIM",
        pageCompression=1,
        allowSplitting=1,
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def _hero(data: dict[str, Any], current: dict[str, Any], category_label: str,
          detail_rows: list[tuple[str, Any]]) -> Any:
    brand = _clean(data.get("brand_name"), "Beauty PIM")
    name = _clean(data.get("product_name"), "Beauty Product")
    density = _DENSITY.get()
    image_w = density.image_width
    text_w = CONTENT_W - image_w - 4 * mm
    image: Any
    try:
        image_data = fetch_public_image(data.get("image_url"))
    except Exception:
        image_data = None
    if image_data:
        image = Image(image_data)
        image._restrictSize(image_w - 4 * mm, density.image_height)
    else:
        image = Table([[_p(brand[:1].upper(), "product")]], colWidths=[image_w - 2 * mm], rowHeights=[density.image_height], style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PALE),
            ("BOX", (0, 0), (-1, -1), .4, LINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]))
    positioning = _clean(current.get("product_positioning"))
    text_rows = [
        [_p(brand.upper(), "brand")],
        [_p(name, "product")],
        [_p(category_label, "meta")],
    ]
    if positioning:
        text_rows.append([_p(positioning, "positioning")])
    text_rows.append([_label_table(detail_rows, text_w, 28 * mm)])
    text = Table(text_rows, colWidths=[text_w], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 1), 1),
        ("BOTTOMPADDING", (0, 2), (-1, 2), 4),
        ("BOTTOMPADDING", (0, 3), (-1, -1), 4),
    ]))
    return Table([[image, text]], colWidths=[image_w, text_w + 4 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 4 * mm),
        ("LEFTPADDING", (1, 0), (1, 0), 0),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))


def _append(story: list[Any], flowable: Any, gap: float | None = None) -> None:
    story.append(KeepTogether(flowable))
    story.append(Spacer(1, _DENSITY.get().gap if gap is None else gap))


def _variant_details(data: dict[str, Any]) -> tuple[str, str]:
    variants = data.get("variants") or []
    variant = variants[0] if variants else {}
    size = " ".join(filter(None, (_clean(variant.get("size")), _clean(variant.get("unit")))))
    gtin = _clean(variant.get("gtin") or data.get("gtin"))
    return size, gtin


def _safe_fragrance_sensory(value: Any, concentration: str) -> str:
    sensory = _clean(value)
    if not sensory:
        return ""
    lower = sensory.lower()
    concentration_lower = concentration.lower()
    if "oil format" in lower and any(term in concentration_lower for term in ("toilette", "parfum", "cologne")):
        return ""
    return sensory


def _inci_paragraph(inci: str) -> Paragraph:
    """Scale long INCI independently so it does not shrink primary content."""
    density = _DENSITY.get()
    size = density.ingredient
    if len(inci) > 1200:
        size = max(8.5, min(size, 8.8))
    elif len(inci) > 650:
        size = max(9, min(size, 9.5))
    return Paragraph(escape(inci), _style("adaptive_inci", size, leading=size * 1.22))


def _build_fragrance_pdf(data: dict[str, Any], current: dict[str, Any]) -> bytes:
    density = _DENSITY.get()
    gap, card_pad = density.gap, density.card_pad_x
    module = current.get("fragrance") if isinstance(current.get("fragrance"), dict) else {}
    size, gtin = _variant_details(data)
    concentration = _clean(module.get("concentration") or current.get("product_type"))
    family = _clean(module.get("fragrance_family"))
    sensory = _safe_fragrance_sensory(current.get("sensory_description"), concentration)
    category_line = " · ".join(filter(None, (concentration, size))) or "Fragrance"
    story: list[Any] = []
    _append(story, _hero(data, current, category_line, [
        ("Fragrance family", family), ("GTIN / EAN", gtin), ("Sensory profile", sensory),
    ]))

    note_width = (CONTENT_W - 2 * gap) / 3
    note_cards = []
    for title, key in (("Top Notes", "top_notes"), ("Heart Notes", "heart_notes"), ("Base Notes", "base_notes")):
        notes = _items(module.get(key))
        content = _bullet_table(notes, note_width - 2 * card_pad,
                                placeholder="Not established from current evidence.")
        note_cards.append(_card(title, content, note_width))
    _append(story, _row(note_cards, [note_width, note_width + gap, note_width + gap]))

    profile_rows = [
        ("Longevity", module.get("longevity")),
        ("Sillage", module.get("sillage_projection")),
        ("Seasonal fit", module.get("seasonal_fit")),
        ("Occasion fit", module.get("occasion_fit")),
    ]
    profile_width = CONTENT_W * .43
    commercial_width = CONTENT_W - profile_width - gap
    profile = _card("Fragrance Profile", _label_table(profile_rows, profile_width - 2 * card_pad), profile_width)
    benefits = _items(current.get("benefits"))[:5]
    benefit_card = _card("Key Benefits", _bullet_table(benefits, commercial_width - 2 * card_pad,
                                                         placeholder="No evidence-grounded benefit recorded."), commercial_width)
    _append(story, _row([profile, benefit_card], [profile_width, commercial_width + gap]))

    directions = _clean(current.get("directions"), "Apply sparingly to pulse points. Do not rub after application.")
    _append(story, _card("How to Use", _p(directions), CONTENT_W))

    audience_value = current.get("target_audience") or {}
    audience = _items(audience_value.get("value") if isinstance(audience_value, dict) else audience_value)[:3]
    _append(story, _card("Three Consumer Profiles", _bullet_table(
        audience, CONTENT_W - 2 * card_pad, numbered=True,
        placeholder="Consumer profiles require stronger product evidence.",
    ), CONTENT_W))

    reviews = _review_lines(data)
    claims = _claims(current)
    if reviews or claims:
        review_claim_lines = reviews + (["Supported claims: " + "; ".join(claims)] if claims else [])
        _append(story, _card("Ratings, Reviews & Claims", _bullet_table(
            review_claim_lines, CONTENT_W - 2 * card_pad,
        ), CONTENT_W))

    formulation = (data.get("formulations") or [{}])[0]
    inci = _clean(formulation.get("raw_inci_text"))
    ingredient_copy = _inci_paragraph(inci) if inci else _p(
        "Ingredient list not available from current evidence.", "placeholder",
    )
    _append(story, _card("Ingredients (INCI)", ingredient_copy, CONTENT_W), 0)
    return _document(f"{_clean(data.get('product_name'), 'Fragrance')} - Fragrance Dossier", story)


def _category_profile(module_name: str, module: dict[str, Any], concerns: list[str]) -> tuple[str, list[tuple[str, Any]]]:
    if module_name == "skincare":
        skin_types = module.get("skin_types") or {}
        return "Skincare Profile", [
            ("Skin types", skin_types.get("recommended_for") if isinstance(skin_types, dict) else skin_types),
            ("Texture", module.get("texture")), ("Finish", module.get("finish")),
            ("Targeted concerns", concerns),
        ]
    if module_name == "haircare":
        hair_types = module.get("hair_types") or {}
        return "Haircare Profile", [
            ("Hair types", hair_types.get("recommended_for") if isinstance(hair_types, dict) else hair_types),
            ("Texture / format", module.get("texture_format")), ("Targeted concerns", concerns),
        ]
    return "Makeup Profile", [
        ("Shade / colour", module.get("shade_colour")), ("Coverage", module.get("coverage")),
        ("Finish", module.get("finish")), ("Texture / format", module.get("texture_format")),
    ]


def _ingredient_name(item: dict[str, Any]) -> str:
    return (_clean(item.get("normalized_inci_name")) or _clean(item.get("ingredient_name"))
            or _clean(item.get("name"), "Key ingredient"))


def _build_category_pdf(data: dict[str, Any], current: dict[str, Any], module_name: str) -> bytes:
    density = _DENSITY.get()
    gap, card_pad = density.gap, density.card_pad_x
    module = current.get(module_name) if isinstance(current.get(module_name), dict) else {}
    size, gtin = _variant_details(data)
    product_type = _clean(current.get("product_type") or data.get("product_type"))
    category_line = " · ".join(filter(None, (product_type, size))) or module_name.title()
    concerns_value = current.get("targeted_concerns") or {}
    concerns = _items(concerns_value.get("values") if isinstance(concerns_value, dict) else concerns_value)
    profile_title, profile_rows = _category_profile(module_name, module, concerns)
    sensory = _clean(current.get("sensory_description"))
    story: list[Any] = []
    _append(story, _hero(data, current, category_line, [
        ("Category", _clean(data.get("product_category"), module_name.title())),
        ("GTIN / EAN", gtin), ("Sensory profile", sensory),
    ]))

    profile_width = CONTENT_W * .42
    benefits_width = CONTENT_W - profile_width - gap
    profile = _card(profile_title, _label_table(profile_rows, profile_width - 2 * card_pad), profile_width)
    benefits = _items(current.get("benefits"))[:5]
    benefit_card = _card("Key Benefits", _bullet_table(
        benefits, benefits_width - 2 * card_pad, placeholder="No evidence-grounded benefit recorded.",
    ), benefits_width)
    _append(story, _row([profile, benefit_card], [profile_width, benefits_width + gap]))

    directions = _clean(current.get("directions"))
    if directions:
        _append(story, _card("How to Use", _p(directions), CONTENT_W))

    audience_value = current.get("target_audience") or {}
    audience = _items(audience_value.get("value") if isinstance(audience_value, dict) else audience_value)[:3]
    _append(story, _card("Three Consumer Profiles", _bullet_table(
        audience, CONTENT_W - 2 * card_pad, numbered=True,
        placeholder="Consumer profiles require stronger product evidence.",
    ), CONTENT_W))

    key_ingredients = current.get("ingredients_intelligence") or data.get("key_ingredients") or []
    if key_ingredients:
        ingredient_rows = []
        for item in key_ingredients[:6]:
            if not isinstance(item, dict):
                continue
            utility = _clean(item.get("benefits") or item.get("functions") or item.get("description"))
            ingredient_rows.append((_ingredient_name(item), utility))
        if ingredient_rows:
            _append(story, _card("Key Ingredients", _label_table(
                ingredient_rows, CONTENT_W - 2 * card_pad, 40 * mm,
            ), CONTENT_W))

    reviews = _review_lines(data)
    claims = _claims(current)
    if reviews or claims:
        heading = "Ratings, Reviews & Claims" if module_name == "skincare" else "Ratings & Reviews"
        review_claim_lines = reviews + (["Supported claims: " + "; ".join(claims)] if claims else [])
        _append(story, _card(heading, _bullet_table(
            review_claim_lines, CONTENT_W - 2 * card_pad,
        ), CONTENT_W))

    formulation = (data.get("formulations") or [{}])[0]
    inci = _clean(formulation.get("raw_inci_text"))
    ingredient_copy = _inci_paragraph(inci) if inci else _p(
        "Ingredient list not available from current evidence.", "placeholder",
    )
    _append(story, _card("Ingredients (INCI)", ingredient_copy, CONTENT_W), 0)
    title = f"{_clean(data.get('product_name'), 'Beauty Product')} - {module_name.title()} Dossier"
    return _document(title, story)


def _select_density(data: dict[str, Any], current: dict[str, Any]) -> DensityPreset:
    """Choose semantic typography/spacing from meaningful content volume."""
    evidence = {
        "description": data.get("description"),
        "current": current,
        "formulations": data.get("formulations"),
        "key_ingredients": data.get("key_ingredients"),
        "market_observations": data.get("market_observations"),
    }
    load = len(json.dumps(evidence, default=str, ensure_ascii=True))
    if load < 1200:
        return DENSITY_PRESETS["low"]
    if load < 2400:
        return DENSITY_PRESETS["medium"]
    return DENSITY_PRESETS["high"]


def _render_category(data: dict[str, Any], current: dict[str, Any], module_name: str) -> bytes:
    if module_name == "fragrance":
        return _build_fragrance_pdf(data, current)
    if module_name in {"skincare", "haircare", "makeup"}:
        return _build_category_pdf(data, current, module_name)
    return _build_category_pdf(data, current, "skincare")


def build_product_pdf(product: Any) -> bytes:
    """Render a natural-height product sheet with adaptive semantic density."""
    data = product.model_dump(mode="json") if hasattr(product, "model_dump") else dict(product)
    current = _current_fields(data)
    modules = {name: current.get(name) for name in ("skincare", "haircare", "makeup", "fragrance")}
    module_name = next((name for name, value in modules.items() if isinstance(value, dict)), "")
    preferred = _select_density(data, current)
    order = ("low", "medium", "high")
    start = order.index(preferred.name)
    result = b""
    for name in order[start:]:
        token = _DENSITY.set(DENSITY_PRESETS[name])
        try:
            result = _render_category(data, current, module_name)
        finally:
            _DENSITY.reset(token)
        # ReportLab emits one /Type /Page entry per page plus /Type /Pages.
        if result.count(b"/Type /Page\n") <= 1:
            break
    return result
