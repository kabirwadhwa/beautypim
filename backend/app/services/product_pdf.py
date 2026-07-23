from html import escape
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.image_urls import fetch_public_image


NAVY = colors.HexColor("#061A4B")
DEEP_BLUE = colors.HexColor("#0B2D70")
COBALT = colors.HexColor("#2563EB")
GOLD = colors.HexColor("#C89B3C")
PALE_BLUE = colors.HexColor("#EDF4FF")
PALE_GOLD = colors.HexColor("#FBF6E9")
INK = colors.HexColor("#172033")
MUTED = colors.HexColor("#64748B")
LINE = colors.HexColor("#CCD6E5")
WHITE = colors.white


def _clean(value: Any, fallback: str = "") -> str:
    if value is None or value == "" or value == []:
        return fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return str(
            value.get("text")
            or value.get("statement")
            or value.get("value")
            or value.get("review_message")
            or value.get("observation_type")
            or fallback
        ).strip()
    if isinstance(value, list):
        return ", ".join(filter(None, (_clean(item) for item in value))) or fallback
    text = str(value).strip()
    return fallback if text.lower() in {"", "unknown", "none", "null", "nan", "not provided"} else text


def _items(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    result = []
    for item in value:
        text = _clean(item)
        if text and text.lower() not in {"not applicable", "not targeted"}:
            result.append(text)
    return result


def _p(value: Any, style: ParagraphStyle, fallback: str = "") -> Paragraph:
    return Paragraph(escape(_clean(value, fallback)).replace("\n", "<br/>"), style)


def _section_header(title: str, styles: dict, width: float = 184 * mm) -> Table:
    return Table(
        [[Paragraph(escape(title.upper()), styles["section"])]],
        colWidths=[width],
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]),
    )


def _bullet_list(values: list[str], styles: dict, empty: str) -> Table:
    rows = []
    for value in values[:7]:
        rows.append([
            Paragraph("+", styles["check"]),
            _p(value, styles["body"]),
        ])
    if not rows:
        rows = [["", _p(empty, styles["muted"])]]
    return Table(rows, colWidths=[7 * mm, None], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))


def _page_decor(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, A4[1] - 5 * mm, A4[0], 5 * mm, fill=1, stroke=0)
    canvas.setStrokeColor(LINE)
    canvas.line(13 * mm, 13 * mm, 197 * mm, 13 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(13 * mm, 8 * mm, "BEAUTY PIM  |  PRODUCT INTELLIGENCE SHEET")
    canvas.drawRightString(197 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build_product_pdf(product: Any) -> bytes:
    data = product.model_dump(mode="json") if hasattr(product, "model_dump") else dict(product)
    current = {
        item["field_name"]: item.get("value")
        for item in data.get("field_values", [])
        if item.get("is_current")
    }

    def field(name: str, fallback: Any = None) -> Any:
        return current.get(name, fallback)

    sample = getSampleStyleSheet()
    styles = {
        "brand": ParagraphStyle("brand", parent=sample["Normal"], fontName="Helvetica-Bold",
                                fontSize=10, leading=12, tracking=1.1, textColor=COBALT),
        "title": ParagraphStyle("title", parent=sample["Title"], fontName="Helvetica-Bold",
                                fontSize=22, leading=23, textColor=NAVY, alignment=TA_LEFT),
        "tagline": ParagraphStyle("tagline", parent=sample["BodyText"], fontName="Helvetica-Oblique",
                                  fontSize=9, leading=12, textColor=MUTED),
        "body": ParagraphStyle("body", parent=sample["BodyText"], fontSize=8.2, leading=10.6, textColor=INK),
        "body_bold": ParagraphStyle("body_bold", parent=sample["BodyText"], fontName="Helvetica-Bold",
                                    fontSize=8.2, leading=10.6, textColor=INK),
        "small": ParagraphStyle("small", parent=sample["BodyText"], fontSize=7.1, leading=8.8, textColor=INK),
        "micro": ParagraphStyle("micro", parent=sample["BodyText"], fontSize=6.5, leading=7.8, textColor=MUTED),
        "label": ParagraphStyle("label", parent=sample["BodyText"], fontName="Helvetica-Bold",
                                fontSize=7.4, leading=9, textColor=NAVY),
        "section": ParagraphStyle("section", parent=sample["BodyText"], fontName="Helvetica-Bold",
                                  fontSize=8.5, leading=10, tracking=.3, textColor=WHITE),
        "check": ParagraphStyle("check", parent=sample["BodyText"], fontName="Helvetica-Bold",
                                fontSize=9, leading=10, textColor=COBALT, alignment=TA_CENTER),
        "pill": ParagraphStyle("pill", parent=sample["BodyText"], fontName="Helvetica-Bold",
                               fontSize=7.1, leading=8.5, textColor=NAVY, alignment=TA_CENTER),
        "ingredient": ParagraphStyle("ingredient", parent=sample["BodyText"], fontName="Helvetica-Bold",
                                     fontSize=8.5, leading=10, textColor=NAVY),
        "muted": ParagraphStyle("muted", parent=sample["BodyText"], fontSize=7.6, leading=9.5, textColor=MUTED),
    }

    product_name = _clean(data.get("product_name"), "Beauty Product")
    brand = _clean(data.get("brand_name"), "Beauty PIM")
    product_type = _clean(field("product_type"), _clean(field("subcategory"), "Beauty Care"))
    category = _clean(data.get("category_path"), "Beauty & Personal Care")
    texture = _clean(field("texture"))
    description = _clean(data.get("description")) or _clean(field("marketing_description"))
    benefits = _items(field("benefits")) or _items(field("source_claims"))
    directions = _clean(field("directions"), "Use as directed on the product packaging.")
    variants = data.get("variants") or []
    first_variant = variants[0] if variants else {}
    size = " ".join(filter(None, [_clean(first_variant.get("size")), _clean(first_variant.get("unit"))]))
    if not description:
        benefit_phrase = benefits[0].rstrip(".").lower() if benefits else "support a considered beauty ritual"
        texture_phrase = f"{texture.lower()} " if texture else ""
        description = (
            f"{product_name} is a {texture_phrase}{product_type.lower()} created to {benefit_phrase}. "
            f"A refined addition to a modern {category.split('>')[-1].strip().lower()} routine."
        )

    concerns = []
    for concern in data.get("dynamic_concerns", []):
        status = str(concern.get("targeting_status") or "").lower()
        if status not in {"unknown", "not_targeted", "false", "none", "not provided"}:
            concerns.append(str(concern.get("concern_name") or "").replace("_", " ").title())
    if not concerns:
        for name in ("hydration", "anti_ageing", "pigmentation", "acne", "redness", "sensitivity"):
            if field(name) is True:
                concerns.append(name.replace("_", " ").title())

    key_ingredients = data.get("key_ingredients") or []
    inci = _clean((data.get("formulations") or [{}])[0].get("raw_inci_text"))
    inci_items = [part.strip() for part in inci.replace(";", ",").split(",") if part.strip()]

    positive_signals = []
    for label, key in (
        ("Vegan", "vegan"), ("Cruelty Free", "cruelty_free"), ("Paraben Free", "paraben_free"),
        ("Sulfate Free", "sulfate_free"), ("Silicone Free", "silicone_free"), ("Alcohol Free", "alcohol_free"),
    ):
        value = _clean(field(key)).lower()
        if value in {"yes", "true", "confirmed", "explicit"}:
            positive_signals.append(label)
    if _clean(field("fragrance_present")).lower() in {"no", "false"}:
        positive_signals.append("Fragrance Free")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=13 * mm, leftMargin=13 * mm,
        topMargin=12 * mm, bottomMargin=18 * mm,
        title=f"{product_name} - Product Intelligence Sheet",
        author="Beauty PIM",
    )
    story = []

    try:
        image_data = fetch_public_image(data.get("image_url"))
    except Exception:
        image_data = None
    if image_data:
        hero_image = Table(
            [[Image(image_data, width=57 * mm, height=57 * mm, kind="proportional")]],
            colWidths=[59 * mm], rowHeights=[59 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), WHITE),
                ("BOX", (0, 0), (-1, -1), .6, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]),
        )
    else:
        initial = escape(brand[:1].upper())
        hero_image = Table(
            [[Paragraph(
                f"<font size='31' color='#C89B3C'><b>{initial}</b></font><br/>"
                f"<font size='8' color='#061A4B'><b>{escape(brand.upper())}</b></font>",
                styles["pill"],
            )]],
            colWidths=[59 * mm], rowHeights=[59 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), PALE_GOLD),
                ("BOX", (0, 0), (-1, -1), .8, GOLD),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]),
        )

    detail_rows = [
        [Paragraph("PRODUCT TYPE", styles["label"]), _p(product_type, styles["body"])],
        [Paragraph("CATEGORY", styles["label"]), _p(category, styles["body"])],
        [Paragraph("FORMAT", styles["label"]), _p(size, styles["body"], "Standard format")],
        [Paragraph("PRODUCT ID", styles["label"]), _p(data.get("internal_code"), styles["small"])],
        [Paragraph("GTIN / EAN", styles["label"]), _p(data.get("gtin"), styles["body"], "Not supplied")],
    ]
    details = Table(detail_rows, colWidths=[29 * mm, 86 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), .25, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    heading = [
        Paragraph(escape(brand.upper()), styles["brand"]),
        Paragraph(escape(product_name.upper()), styles["title"]),
        Paragraph(escape(description), styles["tagline"]),
        Spacer(1, 3 * mm),
        details,
    ]
    story.append(Table([[hero_image, heading]], colWidths=[64 * mm, 120 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ])))
    story.append(Spacer(1, 3 * mm))

    signal_cells = positive_signals[:4] or [product_type, texture or "Beauty Essential", category.split(">")[-1].strip()]
    while len(signal_cells) < 4:
        signal_cells.append("Beauty PIM Verified")
    story.append(Table(
        [[Paragraph(escape(value.upper()), styles["pill"]) for value in signal_cells[:4]]],
        colWidths=[46 * mm] * 4,
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
            ("BOX", (0, 0), (-1, -1), .5, COBALT),
            ("INNERGRID", (0, 0), (-1, -1), .5, WHITE),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]),
    ))
    story.append(Spacer(1, 3 * mm))

    story.append(Table(
        [[
            [_section_header("Key Benefits", styles, 91.5 * mm),
             _bullet_list(benefits, styles, "Product benefits can be added from source claims or enrichment.")],
            [_section_header("Concerns Targeted", styles, 91.5 * mm),
             _bullet_list(concerns, styles, "Designed as part of a considered beauty routine.")],
        ]],
        colWidths=[92 * mm, 92 * mm],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOX", (0, 0), (-1, -1), .45, LINE),
            ("INNERGRID", (0, 0), (-1, -1), .45, LINE),
        ]),
    ))
    story.append(Spacer(1, 3 * mm))

    ingredient_cards = []
    for ingredient in key_ingredients[:3]:
        name = _clean(ingredient.get("normalized_inci_name")) or _clean(ingredient.get("name"), "Key Ingredient")
        utility = _clean(ingredient.get("benefits")) or _clean(ingredient.get("functions"), "Formula support")
        ingredient_cards.append([
            Paragraph(escape(name.upper()), styles["ingredient"]),
            _p(utility, styles["small"]),
        ])
    while len(ingredient_cards) < 3:
        fallback_name = inci_items[len(ingredient_cards)] if len(inci_items) > len(ingredient_cards) else "Signature Formula"
        ingredient_cards.append([
            Paragraph(escape(fallback_name.upper()), styles["ingredient"]),
            _p("Selected as part of the complete formulation.", styles["small"]),
        ])
    story.append(_section_header("Hero Ingredients and Formula Story", styles))
    story.append(Table(
        [[cell for cell in ingredient_cards]],
        colWidths=[184 / 3 * mm] * 3,
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), PALE_GOLD),
            ("BOX", (0, 0), (-1, -1), .45, LINE),
            ("INNERGRID", (0, 0), (-1, -1), .45, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]),
    ))
    story.append(Spacer(1, 3 * mm))

    sensory_rows = [
        [Paragraph("TEXTURE", styles["label"]), _p(texture, styles["body"], "A refined cosmetic texture")],
        [Paragraph("APPLICATION", styles["label"]), _p(field("application_area"), styles["body"], "Use on the intended application area")],
        [Paragraph("SKIN / HAIR FIT", styles["label"]), _p(
            field("skin_type_fit") or field("hair_type_fit"), styles["body"], "Suitable for the product's intended routine")],
        [Paragraph("FRAGRANCE", styles["label"]), _p(
            field("fragrance_intelligence") or field("fragrance_present"), styles["body"], "See packaging for fragrance details")],
    ]
    story.append(Table(
        [[
            [_section_header("How to Use", styles, 91.5 * mm), _p(directions, styles["body"])],
            [_section_header("Texture and Sensory", styles, 91.5 * mm),
             Table(sensory_rows, colWidths=[27 * mm, 64 * mm], style=TableStyle([
                 ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LINEBELOW", (0, 0), (-1, -1), .25, LINE),
                 ("LEFTPADDING", (0, 0), (-1, -1), 3),
                 ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                 ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
             ]))],
        ]],
        colWidths=[92 * mm, 92 * mm],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOX", (0, 0), (-1, -1), .45, LINE),
            ("INNERGRID", (0, 0), (-1, -1), .45, LINE),
        ]),
    ))
    story.append(Spacer(1, 3 * mm))

    story.append(_section_header("Ingredients (INCI)", styles))
    story.append(Table(
        [[_p(inci, styles["small"], "Full ingredients list not supplied.")]],
        colWidths=[184 * mm],
        style=TableStyle([
            ("BOX", (0, 0), (-1, -1), .45, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]),
    ))
    story.append(Spacer(1, 3 * mm))

    if key_ingredients:
        rows = [[
            Paragraph("INGREDIENT", styles["label"]),
            Paragraph("ROLE IN FORMULA", styles["label"]),
            Paragraph("BEAUTY BENEFIT", styles["label"]),
        ]]
        for ingredient in key_ingredients[:12]:
            rows.append([
                _p(ingredient.get("normalized_inci_name") or ingredient.get("name"), styles["small"]),
                _p(", ".join(ingredient.get("functions") or []), styles["small"], "Formula support"),
                _p(", ".join(ingredient.get("benefits") or []), styles["small"], "Supports the product experience"),
            ])
        story.append(_section_header("Ingredient Drivers", styles))
        story.append(Table(rows, colWidths=[48 * mm, 55 * mm, 81 * mm], repeatRows=1, style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PALE_BLUE),
            ("GRID", (0, 0), (-1, -1), .35, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])))
        story.append(Spacer(1, 3 * mm))

    story.append(Table(
        [[
            Paragraph(
                f"<font color='#C89B3C' size='16'><b>{len(inci_items) or '-'}</b></font><br/>"
                "<font color='#061A4B' size='7'><b>INGREDIENTS LISTED</b></font>",
                styles["pill"],
            ),
            Paragraph(
                f"<font color='#C89B3C' size='16'><b>{len(key_ingredients) or '-'}</b></font><br/>"
                "<font color='#061A4B' size='7'><b>INGREDIENT DRIVERS</b></font>",
                styles["pill"],
            ),
            Paragraph(
                f"<font color='#C89B3C' size='16'><b>{len(benefits) or '-'}</b></font><br/>"
                "<font color='#061A4B' size='7'><b>KEY BENEFITS</b></font>",
                styles["pill"],
            ),
            Paragraph(
                f"<font color='#C89B3C' size='16'><b>{len(concerns) or '-'}</b></font><br/>"
                "<font color='#061A4B' size='7'><b>CONCERNS TARGETED</b></font>",
                styles["pill"],
            ),
        ]],
        colWidths=[46 * mm] * 4,
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PALE_GOLD),
            ("BOX", (0, 0), (-1, -1), .6, GOLD),
            ("INNERGRID", (0, 0), (-1, -1), .4, WHITE),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]),
    ))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "Product information is based on the latest catalogue description, claims and ingredient list. "
        "Formulations and packaging may vary by market; always refer to the product label for final consumer guidance.",
        styles["micro"],
    ))

    doc.build(story, onFirstPage=_page_decor, onLaterPages=_page_decor)
    return buffer.getvalue()
