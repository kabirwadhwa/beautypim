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
from reportlab.platypus import Flowable, Image, Paragraph, Table, TableStyle

from app.services.image_urls import fetch_public_image


NAVY = colors.HexColor("#001D55")
INK = colors.HexColor("#07142D")
MUTED = colors.HexColor("#5D6470")
LINE = colors.HexColor("#B8BEC8")
PALE = colors.HexColor("#F5F7FA")
WHITE = colors.white


class LineIcon(Flowable):
    """Small deterministic vector icon that renders reliably in server PDFs."""

    def __init__(self, kind: str, size: float = 8 * mm):
        super().__init__()
        self.kind, self.width, self.height = kind, size, size

    def draw(self):
        c, w, h = self.canv, self.width, self.height
        c.saveState(); c.setStrokeColor(NAVY); c.setFillColor(NAVY); c.setLineWidth(.7)
        c.circle(w / 2, h / 2, min(w, h) * .42, fill=0, stroke=1)
        if self.kind == "cross":
            c.setLineWidth(1.4); c.line(w*.32, h*.5, w*.68, h*.5); c.line(w*.5, h*.32, w*.5, h*.68)
        elif self.kind == "drop":
            path = c.beginPath(); path.moveTo(w*.5, h*.75); path.curveTo(w*.68,h*.55,w*.68,h*.3,w*.5,h*.27); path.curveTo(w*.32,h*.3,w*.32,h*.55,w*.5,h*.75); c.drawPath(path)
        elif self.kind == "flask":
            c.line(w*.44,h*.7,w*.44,h*.55); c.line(w*.56,h*.7,w*.56,h*.55); c.line(w*.4,h*.7,w*.6,h*.7)
            path=c.beginPath(); path.moveTo(w*.44,h*.55); path.lineTo(w*.3,h*.3); path.lineTo(w*.7,h*.3); path.lineTo(w*.56,h*.55); c.drawPath(path)
        elif self.kind == "sun":
            c.circle(w*.5,h*.5,w*.13,fill=0,stroke=1)
            for x1,y1,x2,y2 in ((.5,.72,.5,.86),(.5,.14,.5,.28),(.14,.5,.28,.5),(.72,.5,.86,.5),(.25,.25,.35,.35),(.65,.65,.75,.75),(.25,.75,.35,.65),(.65,.35,.75,.25)): c.line(w*x1,h*y1,w*x2,h*y2)
        elif self.kind == "moon":
            path=c.beginPath(); path.moveTo(w*.62,h*.72); path.curveTo(w*.3,h*.65,w*.3,h*.3,w*.62,h*.25); path.curveTo(w*.45,h*.36,w*.45,h*.6,w*.62,h*.72); c.drawPath(path)
        else:
            c.circle(w*.5,h*.5,w*.11,fill=0,stroke=1); c.line(w*.35,h*.35,w*.65,h*.65)
        c.restoreState()


def _clean(value: Any, fallback: str = "") -> str:
    if value is None or value == "" or value == []:
        return fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return _clean(
            value.get("text") or value.get("statement") or value.get("value")
            or value.get("review_message") or value.get("values"), fallback
        )
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
         title: str | None = None, header: float = 4.7 * mm, dark_header: bool = True) -> None:
    pdf.setStrokeColor(LINE)
    pdf.setLineWidth(.35)
    pdf.rect(x, y, width, height, fill=0, stroke=1)
    if title:
        if dark_header:
            pdf.setFillColor(NAVY)
            pdf.rect(x, y + height - header, width, header, fill=1, stroke=0)
            title_flowable = _p(title.upper(), "section")
        else:
            pdf.setStrokeColor(LINE)
            pdf.line(x, y + height - header, x + width, y + height - header)
            title_flowable = _p(title.upper(), "center_bold")
        _draw(pdf, title_flowable, x, y + height - header, width, header, pad=.5 * mm, bottom=True)


def _bullets(values: list[str], width: float, limit: int = 6) -> Table:
    rows = [[_p("✓", "center_bold"), _p(value)] for value in values[:limit]]
    if not rows:
        rows = [[_p("—", "center_bold"), _p("Not enriched")]]
    return Table(rows, colWidths=[6 * mm, width - 10 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))


def _icon_bullets(values: list[str], width: float, limit: int = 6) -> Table:
    rows = [[LineIcon("concern", 5 * mm), _p(value)] for value in values[:limit]]
    if not rows:
        rows = [[LineIcon("concern", 5 * mm), _p("Not enriched")]]
    return Table(rows, colWidths=[7 * mm, width - 11 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), .7), ("BOTTOMPADDING", (0, 0), (-1, -1), .7),
    ]))


def _name(item: dict[str, Any]) -> str:
    return _clean(item.get("normalized_inci_name")) or _clean(item.get("ingredient_name")) or _clean(item.get("name"), "Key Ingredient")


def _utility(item: dict[str, Any]) -> str:
    return _clean(item.get("benefits")) or _clean(item.get("functions"), "Not enriched")


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
    category = _clean(data.get("product_category")) or _clean(data.get("category_path"), "Not enriched")
    subcategory = _clean(data.get("subcategory")) or _clean(field("subcategory"), "Not enriched")
    product_type = _clean(data.get("product_type")) or _clean(field("product_type"), subcategory)
    benefits = _items(field("benefits")) or _items(field("source_claims"))
    description = _clean(data.get("description")) or _clean(field("marketing_description"))
    if not description:
        description = "Not enriched"
    variants = data.get("variants") or []
    variant = variants[0] if variants else {}
    size = " ".join(filter(None, (_clean(variant.get("size")), _clean(variant.get("unit"))))) or "Not supplied"
    gtin = _clean(data.get("gtin")) or _clean(variant.get("gtin"), "Not supplied")
    country = _clean(field("brand_origin"), "Not enriched")
    texture = _clean(field("texture"), "Not enriched")
    fragrance_data = field("fragrance_intelligence") or {}
    fragrance = (
        _clean(fragrance_data.get("fragrance_family")) if isinstance(fragrance_data, dict) else ""
    ) or _clean(field("fragrance_present"), "Not enriched")
    directions = _clean(field("directions"), "Not enriched")

    concerns = _items((field("targeted_concerns") or {}).get("values") if isinstance(field("targeted_concerns"), dict) else field("targeted_concerns"))
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
    concerns = concerns or ["Not enriched"]

    formulation = (data.get("formulations") or [{}])[0]
    inci = _clean(formulation.get("raw_inci_text"), "Full ingredient list not supplied.")
    inci_items = [item.strip() for item in inci.replace(";", ",").split(",") if item.strip()]
    key_ingredients = field("ingredients_intelligence") or data.get("key_ingredients") or []
    signals = [label for label, key in (("PARABEN FREE", "paraben_free"), ("SULFATE FREE", "sulfate_free"),
               ("PHTHALATE FREE", "phthalate_free"),
               ("SILICONE FREE", "silicone_free"), ("VEGAN", "vegan"),
               ("CRUELTY FREE", "cruelty_free"), ("ALCOHOL FREE", "alcohol_free")) if _yes(field(key))]
    if _clean(field("fragrance_present")).lower() in {"no", "false"}:
        signals.append("FRAGRANCE FREE")
    signals = (signals or ["NO VERIFIED FORMULATION SIGNALS"])[:3]
    inci_lower = inci.lower()
    present_signals = []
    if "alcohol denat" in inci_lower:
        present_signals.append("Alcohol Denat.")
    if any(term in inci_lower for term in ("parfum", "fragrance", "aroma")) or _yes(field("fragrance_present")):
        present_signals.append("Fragrance")
    presence_line = " & ".join(present_signals) + " present" if present_signals else "No additional presence signal recorded"

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
        [_p("Brand Origin:", "label"), _p(country)], [_p("Launch Year:", "label"), _p(field("launch_year"), fallback="Not enriched")],
    ], colWidths=[28 * mm, center_w - 28 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), .25 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), .25 * mm),
    ]))
    _draw(pdf, specs, center_x, top_y, center_w, 26 * mm, pad=0)

    icon_h = 17 * mm
    _box(pdf, signal_x, top_y + top_h - icon_h, signal_w, icon_h)
    credentials = _items((field("product_credentials") or {}).get("values") if isinstance(field("product_credentials"), dict) else field("product_credentials"))
    tested = [label for label, key in (("DERMATOLOGICALLY TESTED", "dermatologically_tested"),
              ("CLINICALLY TESTED", "clinically_tested"), ("OPHTHALMOLOGICALLY TESTED", "ophthalmologically_tested")) if _yes(field(key))]
    icon_labels = (credentials + tested + [product_type, country, "Not enriched"])[:3]
    icon_kinds = ["flask", "cross", "drop"]
    for index, label in enumerate(icon_labels):
        iw = signal_w / 3
        _draw(pdf, LineIcon(icon_kinds[index], 8 * mm), signal_x + iw * index, top_y + top_h - 10 * mm, iw, 9 * mm, pad=(iw - 8 * mm) / 2)
        _draw(pdf, _p(label.upper(), "center"), signal_x + iw * index, top_y + top_h - icon_h, iw, 7 * mm, pad=.5 * mm, bottom=True)
    form_h = top_h - icon_h - gap
    _box(pdf, signal_x, top_y, signal_w, form_h, "Formulation Signals", dark_header=False)
    for index, label in enumerate(signals):
        sw = signal_w / 3
        signal_copy = Paragraph(f"✓<br/>{escape(label)}", STYLES["center_bold"])
        _draw(pdf, signal_copy, signal_x + index * sw, top_y + 6 * mm, sw, 12 * mm, pad=.5 * mm)
    _draw(pdf, _p(presence_line, "center"), signal_x, top_y, signal_w, 7 * mm, pad=.5 * mm, bottom=True)

    # Benefits, hero ingredients and concerns.
    y, row_h = top_y - gap - 48 * mm, 48 * mm
    _box(pdf, xs[0], y, col_w, row_h, "Key Benefits")
    _draw(pdf, _bullets(benefits, col_w), xs[0], y, col_w, row_h - 4.7 * mm)
    _box(pdf, xs[1], y, col_w, row_h, "Hero Ingredients & Technology")
    technologies = (field("proprietary_technologies") or {}).get("items", []) if isinstance(field("proprietary_technologies"), dict) else []
    ingredients = technologies[:3] or key_ingredients[:3]
    hero_rows = []
    for item in ingredients:
        media: Any = _p(_name(item)[:1].upper(), "initial")
        image_url = _clean(item.get("image_url"))
        if image_url:
            try:
                media = Image(fetch_public_image(image_url), width=14.5 * mm, height=11.5 * mm, kind="proportional")
            except Exception:
                pass
        hero_rows.append([
            media,
            [_p(_name(item).upper(), "ingredient"), _p(_clean(item.get("description")) or _utility(item), "small")],
        ])
    if not hero_rows:
        hero_rows = [[_p("—", "initial"), [_p("NOT ENRICHED", "ingredient"), _p("No hero ingredient or technology is recorded.", "small")]]]
    hero_table = Table(hero_rows, colWidths=[16 * mm, col_w - 20 * mm], rowHeights=[13.2 * mm] * len(hero_rows), style=TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.2 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), .8 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), .8 * mm),
        ("LINEBELOW", (0, 0), (-1, -2), .3, LINE),
    ]))
    _draw(pdf, hero_table, xs[1], y, col_w, row_h - 4.7 * mm, pad=1.5 * mm)
    _box(pdf, xs[2], y, col_w, row_h, "Skin Concerns Targeted")
    _draw(pdf, _icon_bullets(concerns, col_w), xs[2], y, col_w, row_h - 4.7 * mm)

    # Skin-fit, directions and sensory.
    y, row_h = y - gap - 34 * mm, 34 * mm
    _box(pdf, xs[0], y, col_w, row_h, "Skin Type Fit")
    fit_data = field("skin_type_scores") or {}
    fit_scores = fit_data.get("scores", {}) if isinstance(fit_data, dict) else {}
    fit_rows = []
    for label in ["Normal", "Dry", "Very Dry", "Combination", "Oily", "Sensitive"]:
        key = label.lower().replace(" ", "_")
        score = max(0, min(5, int(fit_scores.get(key, 0) or 0)))
        fit_rows.append([_p(label), _p(("● " * score + "o " * (5 - score)).strip(), "center_bold")])
    fit_table = Table(fit_rows, colWidths=[25 * mm, col_w - 29 * mm], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), .2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), .2 * mm),
    ]))
    _draw(pdf, fit_table, xs[0], y, col_w, row_h - 4.7 * mm)
    _box(pdf, xs[1], y, col_w, row_h, "How to Use")
    sequence = _clean(field("application_sequence")) or directions
    routine_step = _clean(field("routine_step"), "Not enriched")
    morning_copy = f"<b>Morning:</b> {escape(sequence)} Step: {escape(routine_step)}."
    evening_copy = f"<b>Evening:</b> {escape(sequence)} Step: {escape(routine_step)}."
    use_table = Table([
        [LineIcon("sun", 7 * mm), Paragraph(morning_copy, STYLES["body"])],
        [LineIcon("moon", 7 * mm), Paragraph(evening_copy, STYLES["body"])],
    ], colWidths=[10 * mm, col_w - 15 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
    ]))
    _draw(pdf, use_table, xs[1], y, col_w, row_h - 4.7 * mm, pad=2 * mm)
    _box(pdf, xs[2], y, col_w, row_h, "Texture & Sensory")
    sensory_rows = [
        [_p("Texture:", "label"), _p(texture)], [_p("Color:", "label"), _p(field("colour"), fallback="Not enriched")],
        [_p("Finish:", "label"), _p(field("finish"), fallback="Not enriched")],
        [_p("Fragrance:", "label"), _p(fragrance)],
        [_p("Absorption:", "label"), _p(field("absorption_profile"), fallback="Not enriched")],
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
    _box(pdf, margin, y, inci_w, row_h, "Ingredients (INCI)", dark_header=False)
    _draw(pdf, _p(inci, "small"), margin, y, inci_w, row_h - 4.7 * mm, pad=2.4 * mm)
    _box(pdf, drivers_x, y, drivers_w, row_h, "Ingredient Drivers (Top)", dark_header=False)
    driver_rows = [[_p("INGREDIENT", "micro"), _p("FUNCTION / BENEFIT", "micro")]] + [
        [_p(_name(item), "micro"), _p(_utility(item), "micro")] for item in key_ingredients[:8]
    ]
    _draw(pdf, _grid_table(driver_rows, [drivers_w * .48, drivers_w * .52], pale_first=True),
          drivers_x, y, drivers_w, row_h - 4.7 * mm, pad=0)

    # Detailed ingredient table.
    y, row_h = y - gap - 38 * mm, 38 * mm
    _box(pdf, margin, y, content_w, row_h, "Key Ingredients Breakdown")
    headers = [
        "INCI NAME", "STANDARD", "COMMON NAME", "CHEMICAL NAME / FUNCTION", "POSITION",
        "SHORT DESCRIPTION", "SOURCE", "GROUP / FUNCTION", "OTHER UTILITY", "CAUTION / NOTES",
    ]
    raw_widths = [20, 19, 19, 24, 11, 29, 18, 20, 20, 18]
    scale = content_w / (sum(raw_widths) * mm)
    detail_rows = [[_p(header, "micro") for header in headers]]
    for index, item in enumerate(key_ingredients[:5], start=1):
        name, utility = _name(item), _utility(item)
        functions = _clean(item.get("functions"), "Not enriched")
        detail_rows.append([
            _p(_clean(item.get("ingredient_name"), name), "micro"),
            _p(_clean(item.get("normalized_inci_name"), name.upper()), "micro"),
            _p(_clean(item.get("common_name"), name), "micro"),
            _p(functions, "micro"),
            _p(str(item.get("inci_position") or index), "micro"),
            _p(_clean(item.get("short_description"), utility), "micro"),
            _p(_clean(item.get("source_origin"), "Not enriched"), "micro"),
            _p(_clean(item.get("ingredient_group"), functions), "micro"),
            _p(_clean(item.get("other_utility"), utility), "micro"),
            _p(_clean(item.get("possible_concerns"), "No recorded caution"), "micro"),
        ])
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
        ("Pregnancy Caution", _clean(field("pregnancy_warning_observation"), "Not enriched")),
        ("Allergen Caution", _clean(field("allergen_warning_observation"), "Not enriched")),
        ("Sensitivity Status", _clean(field("sensitivity_warning_observation"), "Not enriched")),
        ("Regulatory Notes", _clean(field("regulatory_notes"), "Not enriched")),
    ]
    _box(pdf, bx[0], bottom_y, widths[0], bottom_h, "Caution Flags", dark_header=False)
    warning_rows = [[_p(label, "micro"), _p(text, "micro")] for label, text in warnings]
    warning_table = _grid_table(warning_rows, [widths[0] * .38, widths[0] * .62])
    warning_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), PALE)]))
    _draw(pdf, warning_table, bx[0], bottom_y, widths[0], bottom_h - 4.7 * mm, pad=0)

    _box(pdf, bx[1], bottom_y, widths[1], bottom_h, "INCI Stats", dark_header=False)
    stats_data = field("inci_stats") if isinstance(field("inci_stats"), dict) else {}
    stats = [
        ("Total Ingredients", stats_data.get("total_ingredients", len(inci_items))),
        ("Allergens", stats_data.get("allergen_count", 0)),
        ("Fragrance", stats_data.get("fragrance_count", 0)),
        ("Plant Extracts", stats_data.get("plant_extracts", 0)),
        ("Peptides", stats_data.get("peptides", 0)),
        ("Antioxidants", stats_data.get("antioxidants", 0)),
        ("Humectants", stats_data.get("humectants", 0)),
        ("Emollients / Oils", stats_data.get("emollients_oils", 0)),
        ("Preservatives", stats_data.get("preservatives", 0)),
    ]
    stats_rows = [[_p(label, "micro"), _p(str(value), "micro")] for label, value in stats]
    stats_table = _grid_table(stats_rows, [widths[1] * .72, widths[1] * .28])
    stats_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), PALE)]))
    _draw(pdf, stats_table, bx[1], bottom_y, widths[1], bottom_h - 4.7 * mm, pad=0)

    _box(pdf, bx[2], bottom_y, widths[2], bottom_h, "Other Utility", dark_header=False)
    _draw(pdf, _bullets(benefits, widths[2], limit=4), bx[2], bottom_y, widths[2], bottom_h - 4.7 * mm, pad=1.2 * mm)

    enriched_h = min(17 * mm, bottom_h * .28)
    identifier_h = bottom_h - enriched_h - gap
    _box(pdf, bx[3], bottom_y + identifier_h + gap, widths[3], enriched_h, "Enriched At", dark_header=False)
    _draw(pdf, _p(data.get("updated_at"), "micro"), bx[3], bottom_y + identifier_h + gap,
          widths[3], enriched_h - 4.7 * mm, pad=1.2 * mm)
    _box(pdf, bx[3], bottom_y, widths[3], identifier_h, "Product Identifier", dark_header=False)
    identifier_rows = [[_p("Article Number", "micro")], [_p(data.get("internal_code"), "micro")],
                       [_p("Brand", "micro")], [_p(brand, "micro")], [_p("Product", "micro")],
                       [_p(product_name, "micro")], [_p("GTIN / EAN", "micro")], [_p(gtin, "micro")]]
    identifier = _grid_table(identifier_rows, [widths[3]])
    identifier.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), PALE), ("BACKGROUND", (0, 2), (-1, 2), PALE),
                                    ("BACKGROUND", (0, 4), (-1, 4), PALE), ("BACKGROUND", (0, 6), (-1, 6), PALE)]))
    _draw(pdf, identifier, bx[3], bottom_y, widths[3], identifier_h - 4.7 * mm, pad=0)

    _box(pdf, bx[4], bottom_y, widths[4], bottom_h, "Schema.org Structured Data", dark_header=False)
    schema = field("schema_org") or {"status": "Not enriched"}
    _draw(pdf, _p(json.dumps(schema, ensure_ascii=True, separators=(", ", ": ")), "micro"),
          bx[4], bottom_y, widths[4], bottom_h - 4.7 * mm, pad=1.3 * mm)

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 4.7)
    pdf.drawCentredString(page_w / 2, 5.2 * mm,
                          "Information based on the latest catalogue description and ingredient list. Formulations may vary by market.")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
