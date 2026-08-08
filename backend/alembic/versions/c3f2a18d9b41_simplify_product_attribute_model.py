"""simplify product attribute model without destroying field history

Revision ID: c3f2a18d9b41
Revises: 739f78b48808
"""

from collections import defaultdict
from datetime import datetime
import uuid
from alembic import op
import sqlalchemy as sa

revision = "c3f2a18d9b41"
down_revision = "739f78b48808"
branch_labels = None
depends_on = None

LEGACY_CLAIMS = (
    "vegan", "cruelty_free", "paraben_free", "sulfate_free", "silicone_free",
    "alcohol_free", "fragrance_present", "phthalate_free", "dermatologically_tested",
    "clinically_tested", "ophthalmologically_tested",
)
LEGACY_CONCERNS = {
    "hydration": "Dehydration", "anti_ageing": "Fine lines and visible ageing",
    "pigmentation": "Pigmentation", "acne": "Acne", "redness": "Redness",
    "sensitivity": "Sensitivity", "scalp_care": "Scalp care",
    "hair_growth": "Hair thinning", "fragrance": "Personal fragrance",
    "freshness": "Freshness",
}
OBSOLETE = set(LEGACY_CLAIMS) | set(LEGACY_CONCERNS) | {
    "gender_target", "brand_origin", "country_of_manufacture", "launch_year",
    "absorption_profile", "product_credentials", "proprietary_technologies",
    "application_sequence", "skin_type_scores", "inci_stats", "regulatory_notes",
    "pregnancy_warning_observation", "allergen_warning_observation",
    "sensitivity_warning_observation", "texture", "colour", "coverage", "finish",
    "skin_type_fit", "hair_type_fit", "fragrance_intelligence",
}

def _items(value):
    if isinstance(value, dict):
        return list(value.get("values") or [])
    return list(value or []) if isinstance(value, list) else ([] if value in (None, "") else [str(value)])

def _dedupe(values):
    seen, result = set(), []
    for value in values:
        key = str(value).strip().casefold()
        if key and key not in seen:
            seen.add(key); result.append(value)
    return result

def upgrade():
    bind = op.get_bind()
    op.add_column("import_jobs", sa.Column("source_name", sa.String(length=255), nullable=True))
    bind.execute(sa.text("UPDATE import_jobs SET source_name = filename WHERE source_name IS NULL"))
    metadata = sa.MetaData()
    fields = sa.Table("field_values", metadata, autoload_with=bind)
    rows = bind.execute(sa.select(fields).where(fields.c.is_current == sa.true())).mappings().all()
    by_product = defaultdict(dict)
    for row in rows:
        if row["canonical_product_id"]:
            by_product[row["canonical_product_id"]][row["field_name"]] = row

    now = datetime.utcnow()
    def new_id():
        value = uuid.uuid4()
        return str(value) if bind.dialect.name == "sqlite" else value
    for product_id, current in by_product.items():
        def value(name, default=None):
            return current.get(name, {}).get("value", default)
        inserts = {}
        concerns = _items(value("targeted_concerns"))
        for old, label in LEGACY_CONCERNS.items():
            if value(old) is True or str(value(old)).lower() in {"true", "yes", "explicit", "inferred", "targeted"}:
                concerns.append(label)
        if concerns:
            inserts["targeted_concerns"] = {"values": _dedupe(concerns), "value_status": "migrated",
                                             "reasoning_summary": "Consolidated from historical concern fields.", "confidence": 0.7, "evidence": []}

        claims = list(value("claims") or []) if isinstance(value("claims"), list) else []
        for old in LEGACY_CLAIMS:
            row = current.get(old)
            if not row: continue
            raw = row["value"]
            status = row.get("semantic_status") or row.get("review_status") or "unknown"
            if status in {"explicit", "confirmed", "verified"}: status = "source_supported"
            if status not in {"source_supported", "unverified", "conflicting", "unknown"}: status = "unverified"
            claims.append({"name": old.replace("_", " ").title(), "value": raw, "status": status,
                           "evidence": row.get("evidence") or [], "reasoning_summary": row.get("reasoning_summary") or "",
                           "confidence": float(row.get("confidence_score") or 0.5)})
        if claims: inserts["claims"] = claims

        sensory = str(value("sensory_description") or "").strip()
        absorption = str(value("absorption_profile") or "").strip()
        if absorption and absorption.casefold() not in sensory.casefold():
            sensory = ". ".join(filter(None, (sensory.rstrip("."), absorption.rstrip(".")))) + "."
        if sensory: inserts["sensory_description"] = sensory
        directions = value("directions") or {}
        sequence = str(value("application_sequence") or "").strip()
        if sequence:
            if not isinstance(directions, dict): directions = {"text": str(directions)}
            text = str(directions.get("text") or "")
            if sequence.casefold() not in text.casefold(): directions["text"] = " ".join(filter(None, (text, sequence)))
            directions.setdefault("source_status", "migrated"); directions.setdefault("evidence", []); directions.setdefault("confidence", 0.6)
            inserts["directions"] = directions

        warnings = list(value("warnings_considerations") or []) if isinstance(value("warnings_considerations"), list) else []
        for old, kind in (("pregnancy_warning_observation", "pregnancy"), ("allergen_warning_observation", "allergen"),
                          ("sensitivity_warning_observation", "sensitivity")):
            item = value(old)
            if isinstance(item, dict) and item.get("review_required"):
                warnings.append({"type": kind, "observation": item.get("review_message") or "Review required.",
                                 "evidence": item.get("evidence") or [], "source_status": "source_supported" if item.get("observed_items") else "unverified",
                                 "confidence": item.get("confidence") or 0.5})
        regulatory = value("regulatory_notes")
        if regulatory and str(regulatory).lower() not in {"unknown", "unverified", "not provided"}:
            warnings.append({"type": "regulatory", "observation": regulatory, "evidence": [], "source_status": "unverified", "confidence": 0.5})
        if warnings: inserts["warnings_considerations"] = warnings

        ptype = str((value("product_type") or "")).lower()
        if isinstance(value("product_type"), dict): ptype = str(value("product_type").get("value") or "").lower()
        if any(x in ptype for x in ("fragrance", "perfume", "parfum", "eau de")):
            old = value("fragrance_intelligence") or {}
            inserts["fragrance"] = {"concentration": old.get("concentration"), "fragrance_family": old.get("fragrance_family"),
                "top_notes": old.get("top_notes") or [], "heart_notes": old.get("middle_notes") or [], "base_notes": old.get("base_notes") or [],
                "longevity": old.get("longevity_profile"), "sillage_projection": old.get("sillage_projection"),
                "seasonal_fit": old.get("seasonal_fit") or [], "occasion_fit": old.get("occasion_fit") or [],
                "evidence": old.get("evidence") or [], "confidence": old.get("confidence") or 0.5}
        elif any(x in ptype for x in ("hair", "shampoo", "conditioner", "scalp")):
            inserts["haircare"] = {"hair_types": value("hair_type_fit") or {"applicable": True}, "texture_format": value("texture"), "key_ingredients": []}
        elif any(x in ptype for x in ("makeup", "lipstick", "foundation", "concealer", "mascara")):
            inserts["makeup"] = {"shade_colour": value("colour"), "coverage": value("coverage"), "finish": value("finish"), "texture_format": value("texture")}
        else:
            inserts["skincare"] = {"skin_types": value("skin_type_fit") or {"applicable": True}, "texture": value("texture"), "finish": value("finish"), "key_ingredients": []}

        names = set(inserts) | (OBSOLETE & set(current))
        if names:
            bind.execute(fields.update().where(sa.and_(fields.c.canonical_product_id == product_id,
                                                       fields.c.field_name.in_(names), fields.c.is_current == sa.true())).values(is_current=False, updated_at=now))
        for name, item_value in inserts.items():
            source = current.get(name) or next(iter(current.values()))
            bind.execute(fields.insert().values(id=new_id(), canonical_product_id=product_id, product_variant_id=None,
                field_name=name, value=item_value, source_type="deterministic_rule", source_reference="attribute-model-v3-migration",
                confidence_score=0.7, review_status="inferred", enrichment_run_id=None, is_current=True,
                evidence=[], reasoning_summary="Non-destructive consolidation of legacy BeautyPIM attributes.",
                semantic_status="migrated", semantic_status_type="schema_migration", created_at=now, updated_at=now))

def downgrade():
    # Historical legacy rows remain present, but automatic reactivation could
    # overwrite edits made after this migration. Downgrade is intentionally safe/no-op.
    op.drop_column("import_jobs", "source_name")
