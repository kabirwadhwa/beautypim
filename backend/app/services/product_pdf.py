from io import BytesIO
from html import escape
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

from app.services.image_urls import fetch_public_image


NAVY = colors.HexColor("#071A4A")
BLUE = colors.HexColor("#2563EB")
PALE = colors.HexColor("#EFF6FF")
INK = colors.HexColor("#172033")
MUTED = colors.HexColor("#64748B")
LINE = colors.HexColor("#CBD5E1")


def _text(value: Any, fallback: str = "Not provided") -> str:
    if value is None or value == "" or value == []:
        return fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, dict):
                values.append(str(item.get("statement") or item.get("value") or item.get("name") or item))
            else:
                values.append(str(item))
        return ", ".join(values) or fallback
    if isinstance(value, dict):
        return str(value.get("value") or value.get("review_message") or value.get("observation_type") or value)
    return str(value)


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(_text(value)).replace("\n", "<br/>"), style)


def _section(title: str, body: list, styles: dict) -> list:
    return [
        Table(
            [[Paragraph(escape(title.upper()), styles["section"])]],
            colWidths=[184 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]),
        ),
        *body,
        Spacer(1, 3 * mm),
    ]


def _page_decor(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.line(13 * mm, 13 * mm, 197 * mm, 13 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(13 * mm, 8 * mm, "Generated from live Beauty PIM catalogue data")
    canvas.drawRightString(197 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build_product_pdf(product: Any) -> bytes:
    data = product.model_dump(mode="json") if hasattr(product, "model_dump") else dict(product)
    current_fields = {
        field["field_name"]: field
        for field in data.get("field_values", [])
        if field.get("is_current")
    }

    def field(name: str, fallback: Any = None) -> Any:
        record = current_fields.get(name)
        return record.get("value") if record else fallback

    sample = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("title", parent=sample["Title"], fontName="Helvetica-Bold",
                                fontSize=21, leading=23, textColor=NAVY, alignment=TA_CENTER),
        "brand": ParagraphStyle("brand", parent=sample["Normal"], fontName="Helvetica-Bold",
                                fontSize=10, leading=12, textColor=BLUE, alignment=TA_CENTER),
        "body": ParagraphStyle("body", parent=sample["BodyText"], fontSize=8.3, leading=11, textColor=INK),
        "small": ParagraphStyle("small", parent=sample["BodyText"], fontSize=7.2, leading=9, textColor=INK),
        "label": ParagraphStyle("label", parent=sample["BodyText"], fontName="Helvetica-Bold",
                                fontSize=7.5, leading=9, textColor=NAVY),
        "section": ParagraphStyle("section", parent=sample["BodyText"], fontName="Helvetica-Bold",
                                  fontSize=8.5, leading=10, textColor=colors.white),
        "muted": ParagraphStyle("muted", parent=sample["BodyText"], fontSize=7.5, leading=9, textColor=MUTED),
    }

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=13 * mm, leftMargin=13 * mm,
        topMargin=13 * mm, bottomMargin=18 * mm,
        title=f"{data.get('product_name', 'Product')} - Beauty PIM Product Sheet",
        author="Beauty PIM",
    )
    story = []

    image_flowable: Any
    try:
        image_data = fetch_public_image(data.get("image_url"))
    except Exception:
        image_data = None
    if image_data:
        image_flowable = Image(image_data, width=56 * mm, height=56 * mm, kind="proportional")
    else:
        image_flowable = Table(
            [[Paragraph("PRODUCT IMAGE<br/><font size='7'>No image URL available</font>", styles["brand"])]],
            colWidths=[56 * mm], rowHeights=[56 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), 0.8, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]),
        )

    identity_rows = [
        [Paragraph("Product type", styles["label"]), _paragraph(field("product_type"), styles["body"])],
        [Paragraph("Category", styles["label"]), _paragraph(data.get("category_path"), styles["body"])],
        [Paragraph("ICN", styles["label"]), _paragraph(data.get("internal_code"), styles["body"])],
        [Paragraph("GTIN", styles["label"]), _paragraph(data.get("gtin"), styles["body"])],
        [Paragraph("Size", styles["label"]), _paragraph(
            " ".join(filter(None, [
                _text(data.get("variants", [{}])[0].get("size"), "") if data.get("variants") else "",
                _text(data.get("variants", [{}])[0].get("unit"), "") if data.get("variants") else "",
            ])).strip() or None, styles["body"])],
        [Paragraph("Review state", styles["label"]), _paragraph(data.get("review_status"), styles["body"])],
    ]
    identity = Table(identity_rows, colWidths=[27 * mm, 78 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    heading = [
        Paragraph(escape(_text(data.get("brand_name"), "BEAUTY PIM")).upper(), styles["brand"]),
        Paragraph(escape(_text(data.get("product_name"))), styles["title"]),
        Spacer(1, 4 * mm),
        identity,
    ]
    story.append(Table([[image_flowable, heading]], colWidths=[64 * mm, 120 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ])))
    story.append(Spacer(1, 4 * mm))

    summary = data.get("description") or field("description") or field("marketing_description") or "No product description has been recorded."
    story.extend(_section("Product Overview", [_paragraph(summary, styles["body"])], styles))

    benefit_items = field("benefits") or field("source_claims")
    benefits = _text(benefit_items)
    directions = _text(field("directions"))
    concern_rows = []
    for concern in data.get("dynamic_concerns", []):
        if concern.get("targeting_status") not in {"not_targeted", "unknown"}:
            concern_rows.append([
                _paragraph(concern.get("concern_name", "").replace("_", " ").title(), styles["body"]),
                _paragraph(concern.get("targeting_status", "").replace("_", " ").title(), styles["body"]),
                _paragraph(f"{round((concern.get('confidence') or 0) * 100)}%", styles["body"]),
            ])
    if not concern_rows:
        concern_rows = [[_paragraph("No targeted concerns recorded", styles["muted"]), "", ""]]
    concern_table = Table(
        [[Paragraph("Concern", styles["label"]), Paragraph("Targeting", styles["label"]),
          Paragraph("Confidence", styles["label"])], *concern_rows],
        colWidths=[80 * mm, 65 * mm, 39 * mm],
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("GRID", (0, 0), (-1, -1), 0.35, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]),
    )
    story.extend(_section("Benefits and Concerns", [
        Table([
            [Paragraph(f"<b>Key benefits</b><br/>{escape(benefits)}", styles["body"]),
             Paragraph(f"<b>How to use</b><br/>{escape(directions)}", styles["body"])]
        ], colWidths=[92 * mm, 92 * mm], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.35, LINE),
            ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])),
        Spacer(1, 3 * mm),
        concern_table,
    ], styles))

    signal_names = [
        ("Vegan", "vegan"), ("Cruelty free", "cruelty_free"), ("Paraben free", "paraben_free"),
        ("Sulfate free", "sulfate_free"), ("Silicone free", "silicone_free"),
        ("Alcohol free", "alcohol_free"), ("Fragrance present", "fragrance_present"),
        ("Texture", "texture"), ("Application area", "application_area"),
        ("Skin type fit", "skin_type_fit"),
    ]
    signal_rows = []
    for idx in range(0, len(signal_names), 2):
        pair = signal_names[idx:idx + 2]
        row = []
        for label, key in pair:
            row.extend([Paragraph(label, styles["label"]), _paragraph(field(key), styles["body"])])
        while len(row) < 4:
            row.extend(["", ""])
        signal_rows.append(row)
    story.extend(_section("Product and Formulation Signals", [
        Table(signal_rows, colWidths=[28 * mm, 64 * mm, 30 * mm, 62 * mm], style=TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.35, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
    ], styles))

    inci = data.get("formulations", [{}])[0].get("raw_inci_text") if data.get("formulations") else None
    story.extend(_section("Ingredients (INCI)", [_paragraph(inci, styles["small"])], styles))

    ingredient_rows = []
    for ingredient in data.get("key_ingredients", [])[:20]:
        ingredient_rows.append([
            _paragraph(ingredient.get("name"), styles["small"]),
            _paragraph(", ".join(ingredient.get("functions") or []), styles["small"]),
            _paragraph(", ".join(ingredient.get("benefits") or []), styles["small"]),
            _paragraph(
                f"{round((ingredient.get('confidence') or 0) * 100)}%" if ingredient.get("confidence") is not None else None,
                styles["small"],
            ),
        ])
    if ingredient_rows:
        story.extend(_section("Key Ingredient Intelligence", [
            Table(
                [[Paragraph("Ingredient", styles["label"]), Paragraph("Function", styles["label"]),
                  Paragraph("Benefits", styles["label"]), Paragraph("Confidence", styles["label"])],
                 *ingredient_rows],
                colWidths=[42 * mm, 48 * mm, 70 * mm, 24 * mm],
                repeatRows=1,
                style=TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), PALE),
                    ("GRID", (0, 0), (-1, -1), 0.35, LINE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]),
            )
        ], styles))

    issue_rows = []
    for issue in data.get("validation_issues", []):
        if not issue.get("resolved"):
            issue_rows.append([
                _paragraph(issue.get("severity", "").upper(), styles["small"]),
                _paragraph(issue.get("field_name"), styles["small"]),
                _paragraph(issue.get("message"), styles["small"]),
            ])
    if not issue_rows:
        issue_rows = [[_paragraph("CLEAR", styles["small"]), "", _paragraph("No active validation issues.", styles["small"])]]
    story.extend(_section("Quality and Governance", [
        Table(
            [[Paragraph("Severity", styles["label"]), Paragraph("Field", styles["label"]),
              Paragraph("Message", styles["label"])], *issue_rows],
            colWidths=[28 * mm, 38 * mm, 118 * mm],
            repeatRows=1,
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), PALE),
                ("GRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]),
        ),
        Spacer(1, 3 * mm),
        _paragraph(
            f"Enrichment engine: {_text((data.get('enrichment_metadata') or {}).get('provider'))} "
            f"{_text((data.get('enrichment_metadata') or {}).get('model'), '')} | "
            f"Generated from product record updated {_text(data.get('updated_at'))}.",
            styles["muted"],
        ),
    ], styles))

    doc.build(story, onFirstPage=_page_decor, onLaterPages=_page_decor)
    return buffer.getvalue()
