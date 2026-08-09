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
    "hero": _style("hero", 17, bold=True, color=NAVY, leading=17.7),
    "country": _style("country", 7.6, bold=True, color=MUTED),
    "body": _style("body", 6.7, leading=7.9),
    "small": _style("small", 5.8, leading=6.75),
    "micro": _style("micro", 4.75, leading=5.45),
    "label": _style("label", 6.4, bold=True, leading=7.5),
    "section": _style("section", 6.65, bold=True, color=WHITE, leading=7.3, align=TA_CENTER),
    "ingredient": _style("ingredient", 7.35, bold=True, color=NAVY, leading=8.3),
    "center": _style("center", 5.55, leading=6.3, align=TA_CENTER),
    "center_bold": _style("center_bold", 5.6, bold=True, color=NAVY, leading=6.35, align=TA_CENTER),
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


def _bullets(values: list[str], width: float, limit: int = 6,
             fill_height: float | None = None) -> Table:
    rows = [[_p("✓", "center_bold"), _p(value)] for value in values[:limit]]
    if not rows:
        rows = [[_p("—", "center_bold"), _p("Not enriched")]]
    row_heights = [fill_height / len(rows)] * len(rows) if fill_height else None
    return Table(rows, colWidths=[6 * mm, width - 10 * mm], rowHeights=row_heights,
                 style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))


def _icon_bullets(values: list[str], width: float, limit: int = 6,
                  fill_height: float | None = None) -> Table:
    rows = [[LineIcon("concern", 5 * mm), _p(value)] for value in values[:limit]]
    if not rows:
        rows = [[LineIcon("concern", 5 * mm), _p("Not enriched")]]
    row_heights = [fill_height / len(rows)] * len(rows) if fill_height else None
    return Table(rows, colWidths=[7 * mm, width - 11 * mm], rowHeights=row_heights,
                 style=TableStyle([
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
                font_padding: float = .65 * mm,
                row_heights: list[float] | None = None) -> Table:
    commands = [
        ("GRID", (0, 0), (-1, -1), .25, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), font_padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), font_padding),
        ("TOPPADDING", (0, 0), (-1, -1), font_padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), font_padding),
    ]
    if pale_first:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), PALE))
    return Table(rows, colWidths=widths, rowHeights=row_heights,
                 repeatRows=1 if pale_first else 0, style=TableStyle(commands))


def _build_fragrance_pdf(data: dict[str, Any], current: dict[str, Any]) -> bytes:
    """Single-page fragrance dossier: pyramid and commercial intelligence first."""
    field = lambda name, fallback=None: current.get(name, fallback)
    module = field("fragrance") if isinstance(field("fragrance"), dict) else {}
    brand = _clean(data.get("brand_name"), "Beauty PIM")
    name = _clean(data.get("product_name"), "Fragrance")
    variants = data.get("variants") or []
    variant = variants[0] if variants else {}
    size = " ".join(filter(None, (_clean(variant.get("size")), _clean(variant.get("unit"))))) or "Not supplied"
    gtin = _clean(variant.get("gtin") or data.get("gtin"), "Not supplied")
    concentration = _clean(module.get("concentration") or field("product_type"), "Not established")
    family = _clean(module.get("fragrance_family"), "Not established")
    benefits = _items(field("benefits"))[:5]
    audience_value = field("target_audience") or {}
    audience = _items(audience_value.get("value") if isinstance(audience_value, dict) else audience_value)[:3]
    directions = _clean(field("directions"), "Spray onto pulse points such as the wrists and neck. Reapply as desired.")
    formulation = (data.get("formulations") or [{}])[0]
    inci = _clean(formulation.get("raw_inci_text"))
    claims = [str(item.get("name")) for item in (field("claims") or []) if isinstance(item, dict)
              and item.get("status") in {"verified", "source_supported"}
              and str(item.get("value", "")).lower() not in {"no", "false"}]

    buffer = BytesIO(); pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"{name} - Fragrance Dossier"); pdf.setAuthor("Beauty PIM")
    pw, ph = A4; margin, gap = 7 * mm, 2.2 * mm; width = pw - 2 * margin
    hero_h, hero_y = 55 * mm, ph - margin - 55 * mm
    image_w = 55 * mm
    image_data = None
    try: image_data = fetch_public_image(data.get("image_url"))
    except Exception: pass
    if image_data:
        image = Image(image_data); image._restrictSize(image_w - 4 * mm, hero_h - 4 * mm)
        _draw(pdf, image, margin, hero_y, image_w, hero_h, pad=2 * mm)
    else:
        pdf.setFillColor(PALE); pdf.roundRect(margin, hero_y, image_w, hero_h, 2*mm, fill=1, stroke=0)
        _draw(pdf, _p(brand[:1].upper(), "hero"), margin, hero_y, image_w, hero_h, pad=20*mm)
    hero_x = margin + image_w + 4 * mm
    _draw(pdf, Table([
        [_p(brand.upper(), "hero")], [_p(name.upper(), "hero")],
        [_p(f"{concentration}  |  {size}", "country")],
        [_p(field("product_positioning"), "body", "Fragrance positioning not yet established.")],
    ], colWidths=[width-image_w-4*mm], style=TableStyle([
        ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
        ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),2),
    ])), hero_x, hero_y+16*mm, width-image_w-4*mm, hero_h-16*mm, pad=0)
    specs = Table([
        [_p("Fragrance family", "label"), _p(family)], [_p("GTIN / EAN", "label"), _p(gtin)],
        [_p("Sensory profile", "label"), _p(field("sensory_description"), fallback="Not established")],
    ], colWidths=[28*mm, width-image_w-32*mm], style=TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),
        ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1),
    ]))
    _draw(pdf, specs, hero_x, hero_y, width-image_w-4*mm, 18*mm, pad=0)

    y = hero_y - gap - 52*mm
    col = (width - 2*gap)/3
    pyramid = (("Top Notes", module.get("top_notes")), ("Heart Notes", module.get("heart_notes")), ("Base Notes", module.get("base_notes")))
    for idx, (title, values) in enumerate(pyramid):
        x = margin + idx*(col+gap); _box(pdf, x, y, col, 52*mm, title)
        items = _items(values)
        copy = "\n".join(f"• {item}" for item in items) if items else "Not established from current evidence."
        _draw(pdf, _p(copy, "body"), x, y, col, 47.3*mm, pad=3*mm)

    y -= gap + 39*mm
    profile = [
        ("Longevity", _clean(module.get("longevity"), "Not established")),
        ("Sillage / projection", _clean(module.get("sillage_projection"), "Not established")),
        ("Seasonal fit", _clean(module.get("seasonal_fit"), "Not established")),
        ("Occasion fit", _clean(module.get("occasion_fit"), "Not established")),
    ]
    _box(pdf, margin, y, col, 39*mm, "Fragrance Profile")
    _draw(pdf, Table([[_p(k,"label"),_p(v)] for k,v in profile], colWidths=[25*mm,col-29*mm],
                     style=TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),
                                       ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1)])),
          margin, y, col, 34.3*mm, pad=2*mm)
    _box(pdf, margin+col+gap, y, col, 39*mm, "Key Benefits")
    _draw(pdf, _bullets(benefits or ["No evidence-grounded benefits recorded."], col, limit=4), margin+col+gap, y, col, 34.3*mm)
    _box(pdf, margin+2*(col+gap), y, col, 39*mm, "How to Use")
    _draw(pdf, _p(directions), margin+2*(col+gap), y, col, 34.3*mm, pad=3*mm)

    y -= gap + 47*mm
    aud_w = width*.62; right_w = width-aud_w-gap
    _box(pdf, margin, y, aud_w, 47*mm, "Three Consumer Profiles")
    _draw(pdf, _bullets(audience or ["Audience profiles require stronger product evidence."], aud_w, limit=3), margin, y, aud_w, 42.3*mm)
    _box(pdf, margin+aud_w+gap, y, right_w, 47*mm, "Supported Claims")
    _draw(pdf, _bullets(claims or ["No meaningful source-supported claims recorded."], right_w, limit=4), margin+aud_w+gap, y, right_w, 42.3*mm)

    bottom_h = 18*mm if not inci else max(20*mm, y-gap-10*mm)
    bottom_y = y-gap-bottom_h if not inci else 10*mm
    _box(pdf, margin, bottom_y, width, bottom_h, "Ingredients (INCI)", dark_header=False)
    _draw(pdf, _p(inci or "Ingredient list not available from current evidence.", "small"), margin, bottom_y, width, bottom_h-4.7*mm, pad=3*mm)
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 5.2)
    pdf.drawCentredString(pw/2, 5.5*mm, "Evidence-backed product intelligence. Unknown facts are not invented.")
    pdf.showPage(); pdf.save(); return buffer.getvalue()


def _build_compact_category_pdf(data: dict[str, Any], current: dict[str, Any], module_name: str) -> bytes:
    """Category-first haircare/makeup sheet without empty skincare tables."""
    field = lambda name, fallback=None: current.get(name, fallback)
    module = field(module_name) if isinstance(field(module_name), dict) else {}
    brand, name = _clean(data.get("brand_name"), "Beauty PIM"), _clean(data.get("product_name"), "Beauty Product")
    variants = data.get("variants") or []; variant = variants[0] if variants else {}
    size = " ".join(filter(None, (_clean(variant.get("size")), _clean(variant.get("unit"))))) or "Not supplied"
    gtin = _clean(variant.get("gtin") or data.get("gtin"), "Not supplied")
    benefits = _items(field("benefits"))[:5]
    concerns_value = field("targeted_concerns") or {}
    concerns = _items(concerns_value.get("values") if isinstance(concerns_value, dict) else concerns_value)
    audience_value = field("target_audience") or {}
    audience = _items(audience_value.get("value") if isinstance(audience_value, dict) else audience_value)[:3]
    claims = [str(item.get("name")) for item in (field("claims") or []) if isinstance(item, dict)
              and item.get("status") in {"verified", "source_supported"} and str(item.get("value", "")).lower() not in {"no", "false"}]
    inci = _clean(((data.get("formulations") or [{}])[0]).get("raw_inci_text"))
    if module_name == "haircare":
        profile = [
            ("Hair types", _clean((module.get("hair_types") or {}).get("recommended_for"), "Not established")),
            ("Texture / format", _clean(module.get("texture_format"), "Not established")),
            ("Targeted concerns", _clean(concerns, "Not established")),
        ]
        module_title = "Haircare Profile"
    else:
        profile = [
            ("Shade / colour", _clean(module.get("shade_colour"), "Not established")),
            ("Coverage", _clean(module.get("coverage"), "Not established")),
            ("Finish", _clean(module.get("finish"), "Not established")),
            ("Texture / format", _clean(module.get("texture_format"), "Not established")),
        ]
        module_title = "Makeup Profile"
    buffer=BytesIO(); pdf=canvas.Canvas(buffer,pagesize=A4,pageCompression=1); pw,ph=A4
    margin,gap=7*mm,2.2*mm; width=pw-2*margin; hero_y=ph-margin-48*mm
    pdf.setTitle(f"{name} - {module_title}"); pdf.setAuthor("Beauty PIM")
    pdf.setFillColor(PALE); pdf.roundRect(margin,hero_y,48*mm,48*mm,2*mm,fill=1,stroke=0)
    _draw(pdf,_p(brand[:1].upper(),"hero"),margin,hero_y,48*mm,48*mm,pad=17*mm)
    hx=margin+52*mm
    _draw(pdf,Table([[_p(brand.upper(),"hero")],[_p(name.upper(),"hero")],[_p(f"{module_title.upper()}  |  {size}","country")],
                     [_p(field("product_positioning"),"body","Positioning not yet established.")]],colWidths=[width-52*mm],
                    style=TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),2)])),
          hx,hero_y+12*mm,width-52*mm,36*mm,pad=0)
    _draw(pdf,Table([[_p("Product type","label"),_p(field("product_type"),fallback="Not established")],
                     [_p("GTIN / EAN","label"),_p(gtin)],[_p("Sensory","label"),_p(field("sensory_description"),fallback="Not established")]],
                    colWidths=[24*mm,width-80*mm],style=TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),1)])),
          hx,hero_y,width-52*mm,14*mm,pad=0)
    col=(width-2*gap)/3; y=hero_y-gap-52*mm
    for idx,(title,content) in enumerate(((module_title,profile),("Key Benefits",benefits),("Targeted Concerns",concerns))):
        x=margin+idx*(col+gap); _box(pdf,x,y,col,52*mm,title)
        if idx==0:
            flow=Table([[_p(k,"label"),_p(v)] for k,v in content],colWidths=[25*mm,col-29*mm],style=TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),2)]))
        else: flow=_bullets(content or ["Not established from current evidence."],col,limit=5)
        _draw(pdf,flow,x,y,col,47.3*mm,pad=3*mm)
    y-=gap+39*mm
    half=(width-gap)/2
    _box(pdf,margin,y,half,39*mm,"How to Use"); _draw(pdf,_p(field("directions"),fallback="Directions not established."),margin,y,half,34.3*mm,pad=3*mm)
    _box(pdf,margin+half+gap,y,half,39*mm,"Three Consumer Profiles"); _draw(pdf,_bullets(audience or ["Audience profiles require stronger evidence."],half,limit=3),margin+half+gap,y,half,34.3*mm)
    y-=gap+34*mm
    _box(pdf,margin,y,width,34*mm,"Supported Claims"); _draw(pdf,_bullets(claims or ["No meaningful source-supported claims recorded."],width,limit=4),margin,y,width,29.3*mm)
    bottom_h=22*mm if not inci else max(30*mm,y-gap-10*mm)
    bottom_y=y-gap-bottom_h if not inci else 10*mm
    _box(pdf,margin,bottom_y,width,bottom_h,"Ingredients (INCI)",dark_header=False)
    _draw(pdf,_p(inci or "Ingredient list not available from current evidence.","small"),margin,bottom_y,width,bottom_h-4.7*mm,pad=3*mm)
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica",5.2); pdf.drawCentredString(pw/2,5.5*mm,"Evidence-backed product intelligence. Unknown facts are not invented.")
    pdf.showPage(); pdf.save(); return buffer.getvalue()


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
    modules = {name: field(name) for name in ("skincare", "haircare", "makeup", "fragrance")}
    module_name = next((name for name, value in modules.items() if isinstance(value, dict)), "")
    if module_name == "fragrance":
        return _build_fragrance_pdf(data, current)
    if module_name in {"haircare", "makeup"}:
        return _build_compact_category_pdf(data, current, module_name)
    module = modules.get(module_name) or {}
    texture = _clean(module.get("texture") or module.get("texture_format"), "Not enriched")
    fragrance_data = modules.get("fragrance") or {}
    fragrance = (
        _clean(fragrance_data.get("fragrance_family")) if isinstance(fragrance_data, dict) else ""
    ) or "Not enriched"
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
    claims = field("claims") or []
    signals = [str(item.get("name") or "").upper() for item in claims if isinstance(item, dict)
               and item.get("status") in {"verified", "source_supported"}
               and str(item.get("value", "")).lower() not in {"no", "false"}]
    signals = (signals or ["NO VERIFIED FORMULATION SIGNALS"])[:3]
    inci_lower = inci.lower()
    present_signals = []
    if "alcohol denat" in inci_lower:
        present_signals.append("Alcohol Denat.")
    if any(term in inci_lower for term in ("parfum", "fragrance", "aroma")):
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

    hero = Table([[_p(brand.upper(), "hero")], [_p(product_name.upper(), "hero")], [_p(category.upper(), "country")]],
                 colWidths=[center_w], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), .3 * mm),
    ]))
    _draw(pdf, hero, center_x, top_y + 26 * mm, center_w, 23 * mm, pad=0)
    specs = Table([
        [_p("Product Type:", "label"), _p(product_type)], [_p("Category:", "label"), _p(category)],
        [_p("Consistency:", "label"), _p(texture)], [_p("Size:", "label"), _p(size)],
        [_p("Article Number:", "label"), _p(data.get("internal_code"))],
        [_p("Subcategory:", "label"), _p(subcategory)], [_p("Application Area:", "label"), _p(field("application_area"), fallback="Not enriched")],
    ], colWidths=[28 * mm, center_w - 28 * mm], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), .25 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), .25 * mm),
    ]))
    _draw(pdf, specs, center_x, top_y, center_w, 26 * mm, pad=0)

    icon_h = 17 * mm
    _box(pdf, signal_x, top_y + top_h - icon_h, signal_w, icon_h)
    icon_labels = (signals + [product_type, category, "No additional verified claim"])[:3]
    icon_kinds = ["flask", "cross", "drop"]
    for index, label in enumerate(icon_labels):
        iw = signal_w / 3
        _draw(pdf, LineIcon(icon_kinds[index], 8 * mm), signal_x + iw * index, top_y + top_h - 10 * mm, iw, 9 * mm, pad=(iw - 8 * mm) / 2)
        _draw(pdf, _p(label.upper(), "center"), signal_x + iw * index, top_y + top_h - icon_h, iw, 7 * mm, pad=.5 * mm, bottom=True)
    form_h = top_h - icon_h - gap
    _box(pdf, signal_x, top_y, signal_w, form_h, "Formulation Signals", dark_header=False)
    signal_count = max(1, len(signals))
    for index, label in enumerate(signals):
        sw = signal_w / signal_count
        signal_copy = Paragraph(f"✓<br/>{escape(label)}", STYLES["center_bold"])
        _draw(pdf, signal_copy, signal_x + index * sw, top_y + 6 * mm, sw, 12 * mm, pad=.5 * mm)
    _draw(pdf, _p(presence_line, "center"), signal_x, top_y, signal_w, 7 * mm, pad=.5 * mm, bottom=True)

    # Benefits, hero ingredients and concerns.
    y, row_h = top_y - gap - 48 * mm, 48 * mm
    _box(pdf, xs[0], y, col_w, row_h, "Key Benefits")
    panel_fill_h = row_h - 8.7 * mm
    _draw(pdf, _bullets(benefits, col_w, fill_height=panel_fill_h),
          xs[0], y, col_w, row_h - 4.7 * mm)
    _box(pdf, xs[1], y, col_w, row_h, "Hero Ingredients & Technology")
    ingredients = key_ingredients[:3]
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
    _box(pdf, xs[2], y, col_w, row_h, "Targeted Concerns")
    _draw(pdf, _icon_bullets(concerns, col_w, fill_height=panel_fill_h),
          xs[2], y, col_w, row_h - 4.7 * mm)

    # Skin-fit, directions and sensory.
    y, row_h = y - gap - 34 * mm, 34 * mm
    _box(pdf, xs[0], y, col_w, row_h, "Hair Type Fit" if module_name == "haircare" else "Skin Type Fit" if module_name == "skincare" else "Category Profile")
    fit = module.get("skin_types") or module.get("hair_types") or {}
    fit_values = _items(fit.get("recommended_for") if isinstance(fit, dict) else fit)
    fit_rows = [[_p(label), _p("Suitable", "center_bold")] for label in (fit_values or ["Not enriched"])]
    fit_height = (row_h - 8.7 * mm) / len(fit_rows)
    fit_table = Table(fit_rows, colWidths=[25 * mm, col_w - 29 * mm],
                      rowHeights=[fit_height] * len(fit_rows), style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), .2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), .2 * mm),
    ]))
    _draw(pdf, fit_table, xs[0], y, col_w, row_h - 4.7 * mm)
    _box(pdf, xs[1], y, col_w, row_h, "How to Use")
    sequence = directions
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
        [_p("Texture:", "label"), _p(texture)], [_p("Color / Shade:", "label"), _p(module.get("shade_colour"), fallback="Not enriched")],
        [_p("Finish:", "label"), _p(module.get("finish"), fallback="Not enriched")],
        [_p("Fragrance:", "label"), _p(fragrance)],
        [_p("Sensory:", "label"), _p(field("sensory_description"), fallback="Not enriched")],
    ]
    sensory_height = (row_h - 8.7 * mm) / len(sensory_rows)
    sensory = Table(sensory_rows, colWidths=[18 * mm, col_w - 22 * mm],
                    rowHeights=[sensory_height] * len(sensory_rows), style=TableStyle([
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
    inci_content_h = row_h - 4.7 * mm
    if 1 < len(inci_items) <= 16:
        columns = 4
        ingredient_rows = []
        for start in range(0, len(inci_items), columns):
            cells = [_p(f"{index + 1:02d}  {name}", "small")
                     for index, name in enumerate(inci_items[start:start + columns], start=start)]
            cells.extend([_p("")] * (columns - len(cells)))
            ingredient_rows.append(cells)
        raw_copy = Paragraph(f"<b>Exact formulation:</b> {escape(inci)}", STYLES["body"])
        formula_rows = [[raw_copy] + [_p("")] * (columns - 1)] + ingredient_rows
        raw_h = 13 * mm
        index_h = max(4 * mm, (inci_content_h - raw_h) / max(1, len(ingredient_rows)))
        formula_table = Table(
            formula_rows, colWidths=[(inci_w - 4 * mm) / columns] * columns,
            rowHeights=[raw_h] + [index_h] * len(ingredient_rows),
            style=TableStyle([
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 1), (-1, -1), PALE),
                ("GRID", (0, 1), (-1, -1), .3, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1.4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), .7 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), .7 * mm),
            ]),
        )
        _draw(pdf, formula_table, margin, y, inci_w, inci_content_h, pad=2 * mm)
    else:
        _draw(pdf, _p(inci, "body"), margin, y, inci_w, inci_content_h, pad=2.4 * mm)
    _box(pdf, drivers_x, y, drivers_w, row_h, "Ingredient Drivers (Top)", dark_header=False)
    driver_rows = [[_p("INGREDIENT", "micro"), _p("FUNCTION / BENEFIT", "micro")]] + [
        [_p(_name(item), "micro"), _p(_utility(item), "micro")] for item in key_ingredients[:8]
    ]
    driver_height = (row_h - 4.7 * mm) / max(1, len(driver_rows))
    _draw(pdf, _grid_table(
        driver_rows, [drivers_w * .48, drivers_w * .52], pale_first=True,
        row_heights=[driver_height] * len(driver_rows),
    ),
          drivers_x, y, drivers_w, row_h - 4.7 * mm, pad=0)

    # Detailed ingredient table.
    y, row_h = y - gap - 38 * mm, 38 * mm
    _box(pdf, margin, y, content_w, row_h, "Key Ingredients Breakdown")
    headers = [
        "INCI NAME", "POSITION", "FUNCTIONS", "BENEFITS / UTILITY",
        "SHORT DESCRIPTION", "CAUTION / NOTES",
    ]
    raw_widths = [34, 13, 31, 42, 48, 32]
    scale = content_w / (sum(raw_widths) * mm)
    detail_rows = [[_p(header, "micro") for header in headers]]
    for index, item in enumerate(key_ingredients[:5], start=1):
        name, utility = _name(item), _utility(item)
        functions = _clean(item.get("functions"), "Not enriched")
        detail_rows.append([
            _p(_clean(item.get("ingredient_name"), name), "micro"),
            _p(str(item.get("inci_position") or index), "micro"),
            _p(functions, "micro"),
            _p(utility, "micro"),
            _p(_clean(item.get("short_description"), utility), "micro"),
            _p(_clean(item.get("possible_concerns"), "No recorded caution"), "micro"),
        ])
    detail_height = (row_h - 4.7 * mm) / max(1, len(detail_rows))
    _draw(pdf, _grid_table(
        detail_rows, [width * mm * scale for width in raw_widths], pale_first=True,
        row_heights=[detail_height] * len(detail_rows),
    ),
          margin, y, content_w, row_h - 4.7 * mm, pad=0)

    # Bottom intelligence strip.
    bottom_y, bottom_h = 9 * mm, y - gap - 9 * mm
    proportions = [.235, .17, .17, .16, .265]
    widths = [(content_w - 4 * gap) * value for value in proportions]
    bx = [margin]
    for width in widths[:-1]:
        bx.append(bx[-1] + width + gap)

    warnings = [(str(item.get("type") or "Other").title(), _clean(item.get("observation")))
                for item in (field("warnings_considerations") or []) if isinstance(item, dict)]
    warnings = warnings or [("Status", "No sourced warning observation recorded")]
    _box(pdf, bx[0], bottom_y, widths[0], bottom_h, "Caution Flags", dark_header=False)
    warning_rows = [[_p(label, "micro"), _p(text, "micro")] for label, text in warnings]
    warning_height = (bottom_h - 4.7 * mm) / max(1, len(warning_rows))
    warning_table = _grid_table(
        warning_rows, [widths[0] * .38, widths[0] * .62],
        row_heights=[warning_height] * len(warning_rows),
    )
    warning_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), PALE)]))
    _draw(pdf, warning_table, bx[0], bottom_y, widths[0], bottom_h - 4.7 * mm, pad=0)

    _box(pdf, bx[1], bottom_y, widths[1], bottom_h, "Target Audience", dark_header=False)
    audience_value = field("target_audience") or {}
    audience = _items(audience_value.get("value") if isinstance(audience_value, dict) else audience_value)
    stats = [(str(index), profile) for index, profile in enumerate(audience[:3], 1)] or [("—", "Not enriched")]
    stats_rows = [[_p(label, "micro"), _p(str(value), "micro")] for label, value in stats]
    stats_height = (bottom_h - 4.7 * mm) / max(1, len(stats_rows))
    stats_table = _grid_table(
        stats_rows, [widths[1] * .72, widths[1] * .28],
        row_heights=[stats_height] * len(stats_rows),
    )
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

    _box(pdf, bx[4], bottom_y, widths[4], bottom_h, "Verified Claims", dark_header=False)
    verified_claims = [item for item in claims if isinstance(item, dict) and item.get("status") in {"verified", "source_supported"}]
    _draw(pdf, _p("; ".join(str(item.get("name")) for item in verified_claims) or "No verified claims recorded", "micro"),
          bx[4], bottom_y, widths[4], bottom_h - 4.7 * mm, pad=1.3 * mm)

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 4.7)
    pdf.drawCentredString(page_w / 2, 5.2 * mm,
                          "Information based on the latest catalogue description and ingredient list. Formulations may vary by market.")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
