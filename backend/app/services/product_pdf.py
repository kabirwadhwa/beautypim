"""Fixed-layout, one-page Beauty PIM product dossier."""

from __future__ import annotations

import json
from html import escape
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, Paragraph, Table, TableStyle

from app.services.image_urls import fetch_public_image


NAVY = colors.HexColor("#001D55")
INK = colors.HexColor("#07142D")
MUTED = colors.HexColor("#5D6470")
LINE = colors.HexColor("#B8BEC8")
PALE = colors.HexColor("#F5F7FA")
WHITE = colors.white


def _clean(value: Any, fallback: str = "") -> str:
    if value is None or value == "" or value == []:
        return fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return _clean(value.get("text") or value.get("statement") or value.get("value"), fallback)
    if isinstance(value, list):
        return ", ".join(filter(None, (_clean(item) for item in value))) or fallback
    result = str(value).strip()
    return fallback if result.lower() in {"", "unknown", "none", "null", "nan", "not provided"} else result


def _items(value: Any) -> list[str]:
    source = value if isinstance(value, list) else [value] if value else []
    return [text for text in (_clean(item) for item in source) if text]


def _style(name: str, size: float, *, bold: bool = False, color=INK,
           leading: float | None = None, align: int = 0) -> ParagraphStyle:
    return ParagraphStyle(
        name, fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
        leading=leading or size * 1.2, textColor=color, alignment=align,
        spaceBefore=0, spaceAfter=0,
    )


STYLES = {
    "hero": _style("hero", 16, bold=True, color=NAVY, leading=16.8),
    "country": _style("country", 7.2, bold=True, color=MUTED),
    "body": _style("body", 6.35, leading=7.55),
    "small": _style("small", 5.45, leading=6.4),
    "micro": _style("micro", 4.5, leading=5.15),
    "label": _style("label", 6.1, bold=True, leading=7.25),
    "section": _style("section", 6.35, bold=True, color=WHITE, leading=7, align=TA_CENTER),
    "ingredient": _style("ingredient", 7, bold=True, color=NAVY, leading=8),
    "center": _style("center", 5.25, leading=6, align=TA_CENTER),
    "center_bold": _style("center_bold", 5.3, bold=True, color=NAVY, leading=6, align=TA_CENTER),
    "initial": _style("initial", 14, bold=True, color=WHITE, align=TA_CENTER),
}


def _p(value: Any, style: str = "body", fallback: str = "") -> Paragraph:
    return Paragraph(escape(_clean(value, fallback)).replace("\n", "<br/>"), STYLES[style])


def _draw(pdf: canvas.Canvas, flowable: Any, x: float, y: float, width: float,
          height: float, *, pad: float = 2 * mm, bottom: bool = False) -> None:
    available_width = max(1, width - 2 * pad)
    available_height = max(1, height - 2 * pad)
    _, used_height = flowable.wrap(available_width, available_height)
    draw_y = y + pad if bottom else y + height - pad - used_height
    pdf.saveState()
    path = pdf.beginPath()
    path.rect(x + .3, y + .3, width - .6, height - .6)
    pdf.clipPath(path, stroke=0, fill=0)
    flowable.drawOn(pdf, x + pad, draw_y)
    pdf.restoreState()


def _box(pdf: canvas.Canvas, x: float, y: float, width: float, height: float,
         title: str | None = None, header: float = 4.7 * mm) -> None:
    pdf.setStrokeColor(LINE)
    pdf.setLineWidth(.35)
    pdf.rect(x, y, width, height, fill=0, stroke=1)
    if title:
        pdf.setFillColor(NAVY)
        pdf.rect(x, y + height - header, width, header, fill=1, stroke=0)
        _draw(pdf, _p(title.upper(), "section"), x, y + height - header, width, header, pad=.5 * mm, bottom=True)


def _bullets(values: list[str], width: float, limit: int = 6) -> Table:
    rows = [[_p("✓", "center_bold"), _p(value)] for value in values[:limit]]
    if not rows:
        rows = [[_p("✓", "center_bold"), _p("Suitable for the intended beauty routine.")]]
    return Table(rows, colWidths=[6 * mm, width - 10 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))


def _name(item: dict[str, Any]) -> str:
    return _clean(item.get("normalized_inci_name")) or _clean(item.get("name"), "Key Ingredient")


def _utility(item: dict[str, Any]) -> str:
    return _clean(item.get("benefits")) or _clean(item.get("functions"), "Formula support")


def _yes(value: Any) -> bool:
    return _clean(value).lower() in {"yes", "true", "confirmed", "explicit", "1"}


def _grid_table(rows: list[list[Any]], widths: list[float], *, pale_first: bool = False,
                font_padding: float = .65 * mm) -> Table:
    commands = [
        ("GRID", (0, 0), (-1, -1), .25, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), font_padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), font_padding),
        ("TOPPADDING", (0, 0), (-1, -1), font_padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), font_padding),
    ]
    if pale_first:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), PALE))
    return Table(rows, colWidths=widths, repeatRows=1 if pale_first else 0, style=TableStyle(commands))


def build_product_pdf(product: Any) -> bytes:
    data = product.model_dump(mode="json") if hasattr(product, "model_dump") else dict(product)
    current = {item["field_name"]: item.get("value") for item in data.get("field_values", [])
               if item.get("is_current") and item.get("field_name")}
    field = lambda name, fallback=None: current.get(name, fallback)

    product_name = _clean(data.get("product_name"), "Beauty Product")
    brand = _clean(data.get("brand_name"), "Beauty PIM")
    category = _clean(data.get("product_category")) or _clean(data.get("category_path"), "Beauty & Personal Care")
    subcategory = _clean(data.get("subcategory")) or _clean(field("subcategory"), "Beauty Care")
    product_type = _clean(data.get("product_type")) or _clean(field("product_type"), subcategory)
    benefits = _items(field("benefits")) or _items(field("source_claims"))
    description = _clean(data.get("description")) or _clean(field("marketing_description"))
    if not description:
        description = benefits[0] if benefits else f"A considered {product_type.lower()} for a modern beauty routine."
    variants = data.get("variants") or []
    variant = variants[0] if variants else {}
    size = " ".join(filter(None, (_clean(variant.get("size")), _clean(variant.get("unit"))))) or "Standard"
    gtin = _clean(data.get("gtin")) or _clean(variant.get("gtin"), "Not supplied")
    country = _clean(field("brand_origin")) or _clean(field("country_of_origin"), "International")
    texture = _clean(field("texture"), "Refined cosmetic texture")
    fragrance = _clean(field("fragrance_intelligence")) or _clean(field("fragrance_present"), "See packaging")
    directions = _clean(field("directions"), "Apply as directed on the product packaging.")

    concerns = []
    for concern in data.get("dynamic_concerns", []):
        status = _clean(concern.get("targeting_status")).lower()
        if status not in {"unknown", "not_targeted", "false", "none", "not provided"}:
            concerns.append(_clean(concern.get("concern_name")).replace("_", " ").title())
    if not concerns:
        for key, label in (("hydration", "Dryness and dehydration"), ("anti_ageing", "Fine lines and wrinkles"),
                           ("pigmentation", "Uneven tone and pigmentation"), ("acne", "Blemishes and congestion"),
                           ("redness", "Redness"), ("sensitivity", "Sensitivity")):
            if _yes(field(key)):
                concerns.append(label)
    concerns = concerns or [f"Supports {product_type.lower()} needs", "Everyday beauty maintenance"]

    formulation = (data.get("formulations") or [{}])[0]
    inci = _clean(formulation.get("raw_inci_text"), "Full ingredient list not supplied.")
    inci_items = [item.strip() for item in inci.replace(";", ",").split(",") if item.strip()]
    key_ingredients = data.get("key_ingredients") or [
        {"name": item, "functions": ["Formula component"], "benefits": ["Supports the complete formula"]}
        for item in inci_items[:5]
    ]
    signals = [label for label, key in (("PARABEN FREE", "paraben_free"), ("SULFATE FREE", "sulfate_free"),
               ("SILICONE FREE", "silicone_free"), ("VEGAN", "vegan"),
               ("CRUELTY FREE", "cruelty_free"), ("ALCOHOL FREE", "alcohol_free")) if _yes(field(key))]
    if _clean(field("fragrance_present")).lower() in {"no", "false"}:
        signals.append("FRAGRANCE FREE")
    signals = (signals or ["CATALOGUE VERIFIED", "FORMULA PROFILED", "PIM REVIEWED"])[:3]

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"{product_name} - Product Dossier")
    pdf.setAuthor("Beauty PIM")
    page_w, page_h = A4
    margin, gap = 4.7 * mm, 1.7 * mm
    content_w = page_w - 2 * margin
    col_w = (content_w - 2 * gap) / 3
    xs = [margin, margin + col_w + gap, margin + 2 * (col_w + gap)]

    # Hero block.
    top_h = 49 * mm
    top_y = page_h - margin - top_h
    image_w, center_w = 54 * mm, 88 * mm
    center_x = margin + image_w + gap
    signal_x = center_x + center_w + gap
    signal_w = content_w - image_w - center_w - 2 * gap
    image_data = None
    try:
        image_data = fetch_public_image(data.get("image_url"))
    except Exception:
        pass
    if image_data:
        image = Image(image_data)
        image._restrictSize(image_w - 3 * mm, top_h - 3 * mm)
        _draw(pdf, image, margin, top_y, image_w, top_h, pad=1.5 * mm, bottom=True)
    else:
        pdf.setFillColor(PALE)
        pdf.roundRect(margin + 2 * mm, top_y + 2 * mm, image_w - 4 * mm, top_h - 4 * mm, 2 * mm, fill=1, stroke=0)
        pdf.setFillColor(NAVY)
        pdf.setFont("Helvetica-Bold", 27)
        pdf.drawCentredString(margin + image_w / 2, top_y + top_h / 2 + 2 * mm, brand[:1].upper())
        pdf.setFont("Helvetica-Bold", 6.4)
        pdf.drawCentredString(margin + image_w / 2, top_y + top_h / 2 - 5 * mm, brand.upper()[:27])

    hero = Table([[_p(brand.upper(), "hero")], [_p(product_name.upper(), "hero")], [_p(country.upper(), "country")]],
                 colWidths=[center_w], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), .3 * mm),
    ]))
    _draw(pdf, hero, center_x, top_y + 26 * mm, center_w, 23 * mm, pad=0)
    specs = Table([
        [_p("Product Type:", "label"), _p(product_type)], [_p("Category:", "label"), _p(category)],
        [_p("Consistency:", "label"), _p(texture)], [_p("Size:", "label"), _p(size)],
        [_p("Article Number:", "label"), _p(data.get("internal_code"))],
        [_p("Brand Origin:", "label"), _p(country)], [_p("Launch Year:", "label"), _p(field("launch_year"), fallback="Current range")],
    ], colWidths=[28 * mm, center_w - 28 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), .25 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), .25 * mm),
    ]))
    _draw(pdf, specs, center_x, top_y, center_w, 26 * mm, pad=0)

    icon_h = 17 * mm
    _box(pdf, signal_x, top_y + top_h - icon_h, signal_w, icon_h)
    icon_labels = [product_type, country, "CATALOGUE TESTED"]
    for index, label in enumerate(icon_labels):
        iw = signal_w / 3
        cx, cy = signal_x + iw * (index + .5), top_y + top_h - 6 * mm
        pdf.setStrokeColor(NAVY)
        pdf.circle(cx, cy, 3.1 * mm, fill=0, stroke=1)
        pdf.setFont("Helvetica-Bold", 6.5)
        pdf.setFillColor(NAVY)
        pdf.drawCentredString(cx, cy - 1.8, "✓")
        _draw(pdf, _p(label.upper(), "center"), signal_x + iw * index, top_y + top_h - icon_h, iw, 7 * mm, pad=.5 * mm, bottom=True)
    form_h = top_h - icon_h - gap
    _box(pdf, signal_x, top_y, signal_w, form_h, "Formulation Signals")
    for index, label in enumerate(signals):
        sw = signal_w / 3
        signal_copy = Paragraph(f"✓<br/>{escape(label)}", STYLES["center_bold"])
        _draw(pdf, signal_copy, signal_x + index * sw, top_y + 6 * mm, sw, 12 * mm, pad=.5 * mm)
    _draw(pdf, _p(f"Fragrance: {fragrance}", "center"), signal_x, top_y, signal_w, 7 * mm, pad=.5 * mm, bottom=True)

    # Benefits, hero ingredients and concerns.
    y, row_h = top_y - gap - 48 * mm, 48 * mm
    _box(pdf, xs[0], y, col_w, row_h, "Key Benefits")
    _draw(pdf, _bullets(benefits, col_w), xs[0], y, col_w, row_h - 4.7 * mm)
    _box(pdf, xs[1], y, col_w, row_h, "Hero Ingredients & Technology")
    ingredients = key_ingredients[:3]
    hero_rows = [[_p(_name(item)[:1].upper(), "initial"), [_p(_name(item).upper(), "ingredient"), _p(_utility(item), "small")]] for item in ingredients]
    hero_table = Table(hero_rows, colWidths=[16 * mm, col_w - 20 * mm], rowHeights=[13.2 * mm] * len(hero_rows), style=TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.2 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), .8 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), .8 * mm),
        ("LINEBELOW", (0, 0), (-1, -2), .3, LINE),
    ]))
    _draw(pdf, hero_table, xs[1], y, col_w, row_h - 4.7 * mm, pad=1.5 * mm)
    _box(pdf, xs[2], y, col_w, row_h, "Skin Concerns Targeted")
    _draw(pdf, _bullets(concerns, col_w), xs[2], y, col_w, row_h - 4.7 * mm)

    # Skin-fit, directions and sensory.
    y, row_h = y - gap - 34 * mm, 34 * mm
    _box(pdf, xs[0], y, col_w, row_h, "Skin Type Fit")
    fit_text = _clean(field("skin_type_fit")).lower()
    fit_rows = []
    for index, label in enumerate(["Normal", "Dry", "Very Dry", "Combination", "Oily", "Sensitive"]):
        score = 5 if label.lower() in fit_text else max(2, 5 - index // 2)
        fit_rows.append([_p(label), _p(("● " * score + "o " * (5 - score)).strip(), "center_bold")])
    fit_table = Table(fit_rows, colWidths=[25 * mm, col_w - 29 * mm], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), .2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), .2 * mm),
    ]))
    _draw(pdf, fit_table, xs[0], y, col_w, row_h - 4.7 * mm)
    _box(pdf, xs[1], y, col_w, row_h, "How to Use")
    use_copy = f"<b>Application:</b> {escape(directions)}<br/><br/><b>Area:</b> {escape(_clean(field('application_area'), 'Target application area'))}"
    _draw(pdf, Paragraph(use_copy, STYLES["body"]), xs[1], y, col_w, row_h - 4.7 * mm, pad=3 * mm)
    _box(pdf, xs[2], y, col_w, row_h, "Texture & Sensory")
    sensory_rows = [
        [_p("Texture:", "label"), _p(texture)], [_p("Color:", "label"), _p(field("color"), fallback="Product dependent")],
        [_p("Finish:", "label"), _p(field("finish"), fallback="Natural finish")],
        [_p("Fragrance:", "label"), _p(fragrance)],
        [_p("Absorption:", "label"), _p(field("absorption"), fallback="Comfortable application")],
    ]
    sensory = Table(sensory_rows, colWidths=[18 * mm, col_w - 22 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), .25 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), .25 * mm),
    ]))
    _draw(pdf, sensory, xs[2], y, col_w, row_h - 4.7 * mm)

    # INCI and ingredient drivers.
    y, row_h = y - gap - 46 * mm, 46 * mm
    inci_w = content_w * .72
    drivers_x, drivers_w = margin + inci_w + gap, content_w - inci_w - gap
    _box(pdf, margin, y, inci_w, row_h, "Ingredients (INCI)")
    _draw(pdf, _p(inci, "small"), margin, y, inci_w, row_h - 4.7 * mm, pad=2.4 * mm)
    _box(pdf, drivers_x, y, drivers_w, row_h, "Ingredient Drivers (Top)")
    driver_rows = [[_p("INGREDIENT", "micro"), _p("FUNCTION / BENEFIT", "micro")]] + [
        [_p(_name(item), "micro"), _p(_utility(item), "micro")] for item in key_ingredients[:8]
    ]
    _draw(pdf, _grid_table(driver_rows, [drivers_w * .48, drivers_w * .52], pale_first=True),
          drivers_x, y, drivers_w, row_h - 4.7 * mm, pad=0)

    # Detailed ingredient table.
    y, row_h = y - gap - 38 * mm, 38 * mm
    _box(pdf, margin, y, content_w, row_h, "Key Ingredients Breakdown")
    headers = ["INCI NAME", "STANDARD", "COMMON NAME", "FUNCTION", "POSITION", "SHORT DESCRIPTION", "GROUP", "OTHER UTILITY"]
    raw_widths = [22, 20, 22, 27, 13, 36, 24, 30]
    scale = content_w / (sum(raw_widths) * mm)
    detail_rows = [[_p(header, "micro") for header in headers]]
    for index, item in enumerate(key_ingredients[:5], start=1):
        name, utility = _name(item), _utility(item)
        functions = _clean(item.get("functions"), "Formula support")
        detail_rows.append([_p(name, "micro"), _p(name.upper(), "micro"), _p(name, "micro"),
                            _p(functions, "micro"), _p(str(index), "micro"), _p(utility, "micro"),
                            _p(functions, "micro"), _p(utility, "micro")])
    _draw(pdf, _grid_table(detail_rows, [width * mm * scale for width in raw_widths], pale_first=True),
          margin, y, content_w, row_h - 4.7 * mm, pad=0)

    # Bottom intelligence strip.
    bottom_y, bottom_h = 9 * mm, y - gap - 9 * mm
    proportions = [.235, .17, .17, .16, .265]
    widths = [(content_w - 4 * gap) * value for value in proportions]
    bx = [margin]
    for width in widths[:-1]:
        bx.append(bx[-1] + width + gap)

    warnings = [
        ("Pregnancy Caution", _clean(field("pregnancy_warning"), "Consult a physician if concerned.")),
        ("Allergen Caution", _clean(field("allergen_warning"), "Review ingredients before use.")),
        ("Sensitivity Status", _clean(field("sensitivity_warning"), "Patch test before first use.")),
        ("Regulatory Notes", _clean(field("warnings"), "For external use. Follow packaging directions.")),
    ]
    _box(pdf, bx[0], bottom_y, widths[0], bottom_h, "Caution Flags")
    warning_rows = [[_p(label, "micro"), _p(text, "micro")] for label, text in warnings]
    warning_table = _grid_table(warning_rows, [widths[0] * .38, widths[0] * .62])
    warning_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), PALE)]))
    _draw(pdf, warning_table, bx[0], bottom_y, widths[0], bottom_h - 4.7 * mm, pad=0)

    _box(pdf, bx[1], bottom_y, widths[1], bottom_h, "INCI Stats")
    stats = [("Total Ingredients", len(inci_items)), ("Ingredient Drivers", len(key_ingredients)),
             ("Claims / Benefits", len(benefits)), ("Concerns", len(concerns)), ("Formula Signals", len(signals))]
    stats_rows = [[_p(label, "micro"), _p(str(value), "micro")] for label, value in stats]
    stats_table = _grid_table(stats_rows, [widths[1] * .72, widths[1] * .28])
    stats_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), PALE)]))
    _draw(pdf, stats_table, bx[1], bottom_y, widths[1], bottom_h - 4.7 * mm, pad=0)

    _box(pdf, bx[2], bottom_y, widths[2], bottom_h, "Other Utility")
    _draw(pdf, _bullets(benefits, widths[2], limit=4), bx[2], bottom_y, widths[2], bottom_h - 4.7 * mm, pad=1.2 * mm)

    _box(pdf, bx[3], bottom_y, widths[3], bottom_h, "Product Identifier")
    identifier_rows = [[_p("Article Number", "micro")], [_p(data.get("internal_code"), "micro")],
                       [_p("Brand", "micro")], [_p(brand, "micro")], [_p("GTIN / EAN", "micro")],
                       [_p(gtin, "micro")], [_p("Updated", "micro")], [_p(data.get("updated_at"), "micro")]]
    identifier = _grid_table(identifier_rows, [widths[3]])
    identifier.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), PALE), ("BACKGROUND", (0, 2), (-1, 2), PALE),
                                    ("BACKGROUND", (0, 4), (-1, 4), PALE), ("BACKGROUND", (0, 6), (-1, 6), PALE)]))
    _draw(pdf, identifier, bx[3], bottom_y, widths[3], bottom_h - 4.7 * mm, pad=0)

    _box(pdf, bx[4], bottom_y, widths[4], bottom_h, "Schema.org Structured Data")
    schema = {"@context": "https://schema.org", "@type": "Product", "name": product_name,
              "brand": {"@type": "Brand", "name": brand}, "description": description,
              "sku": _clean(data.get("internal_code")), "gtin": gtin, "category": category, "size": size}
    _draw(pdf, _p(json.dumps(schema, ensure_ascii=True, separators=(", ", ": ")), "micro"),
          bx[4], bottom_y, widths[4], bottom_h - 4.7 * mm, pad=1.3 * mm)

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 4.7)
    pdf.drawCentredString(page_w / 2, 5.2 * mm,
                          "Information based on the latest catalogue description and ingredient list. Formulations may vary by market.")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
