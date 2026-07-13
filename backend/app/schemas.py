from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Any, Dict
from datetime import datetime, date
import uuid

# User Schemas
class UserBase(BaseModel):
    email: EmailStr

class UserCreate(UserBase):
    password: str

class UserLogin(UserBase):
    password: str

class UserOut(UserBase):
    id: uuid.UUID
    role: str
    created_at: datetime

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str
    role: str

class TokenData(BaseModel):
    email: Optional[str] = None

# Mapping Templates
class MappingTemplateBase(BaseModel):
    name: str
    source_name: str
    file_type: str
    column_mapping: Dict[str, str]
    transformation_rules: Optional[Dict[str, Any]] = None

class MappingTemplateCreate(MappingTemplateBase):
    pass

class MappingTemplateOut(MappingTemplateBase):
    id: uuid.UUID
    created_by_id: Optional[uuid.UUID]
    created_at: datetime

    class Config:
        from_attributes = True

# Import Job
class ImportJobOut(BaseModel):
    id: uuid.UUID
    filename: str
    file_hash: str
    status: str
    total_rows: int
    processed_rows: int
    error_message: Optional[str] = None
    column_mapping: Dict[str, str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class ImportJobItemOut(BaseModel):
    id: uuid.UUID
    import_job_id: uuid.UUID
    source_row_number: int
    source_listing_id: Optional[uuid.UUID] = None
    canonical_product_id: Optional[uuid.UUID] = None
    product_variant_id: Optional[uuid.UUID] = None
    status: str
    match_status: str
    duplicate_score: float
    enrichment_status: str
    retry_count: int
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

# Structured AI Inferences
class EvidenceItemSchema(BaseModel):
    source_reference: Optional[str] = None
    source_field: str
    supporting_text: str
    evidence_type: str
    char_offsets: Optional[str] = None

class CategoricalFieldSchema(BaseModel):
    value: Optional[str] = None
    value_status: str
    evidence: List[EvidenceItemSchema] = []
    reasoning_summary: str
    confidence: float

class ClaimFieldSchema(BaseModel):
    value: str
    claim_status: str
    evidence: List[EvidenceItemSchema] = []
    reasoning_summary: str
    confidence: float

class ConcernFieldSchema(BaseModel):
    targeting_status: str
    evidence: List[EvidenceItemSchema] = []
    reasoning_summary: str
    confidence: float

class BenefitSchema(BaseModel):
    statement: str
    source_type: str
    evidence: str
    confidence: float

class DirectionsSchema(BaseModel):
    text: Optional[str] = None
    source_status: str
    evidence: List[EvidenceItemSchema] = []
    confidence: Optional[float] = None

class SkinTypeFitSchema(BaseModel):
    applicable: bool
    recommended_for: List[str] = []
    not_recommended_for: List[str] = []
    unknown_for: List[str] = []
    evidence: List[EvidenceItemSchema] = []
    confidence: Optional[float] = None

class HairTypeFitSchema(BaseModel):
    applicable: bool
    recommended_for: List[str] = []
    not_recommended_for: List[str] = []
    unknown_for: List[str] = []
    evidence: List[EvidenceItemSchema] = []
    confidence: Optional[float] = None

class FragranceIntelligenceSchema(BaseModel):
    applicable: bool
    fragrance_presence_status: str
    fragrance_family: Optional[str] = None
    top_notes: List[str] = []
    middle_notes: List[str] = []
    base_notes: List[str] = []
    evidence: List[EvidenceItemSchema] = []
    confidence: Optional[float] = None

class ReviewObservationSchema(BaseModel):
    observation_domain: str
    review_required: bool
    observation_type: str
    observed_items: List[str] = []
    evidence: List[EvidenceItemSchema] = []
    review_message: str
    confidence: float

class IngredientIntelligenceSchema(BaseModel):
    ingredient_name: str
    normalized_inci_name: Optional[str] = None
    functions: List[str] = []
    benefits: List[str] = []
    possible_concerns: List[Dict[str, Any]] = []
    is_key_ingredient: bool
    key_ingredient_status: str
    evidence: List[EvidenceItemSchema] = []
    confidence: float

class BeautyProductEnrichmentSchema(BaseModel):
    subcategory: CategoricalFieldSchema
    product_type: CategoricalFieldSchema
    gender_target: CategoricalFieldSchema
    texture: CategoricalFieldSchema
    application_area: CategoricalFieldSchema
    target_audience: CategoricalFieldSchema
    
    vegan: ClaimFieldSchema
    cruelty_free: ClaimFieldSchema
    paraben_free: ClaimFieldSchema
    sulfate_free: ClaimFieldSchema
    silicone_free: ClaimFieldSchema
    alcohol_free: ClaimFieldSchema
    fragrance_present: ClaimFieldSchema
    
    hydration: ConcernFieldSchema
    anti_ageing: ConcernFieldSchema
    pigmentation: ConcernFieldSchema
    acne: ConcernFieldSchema
    redness: ConcernFieldSchema
    sensitivity: ConcernFieldSchema
    scalp_care: ConcernFieldSchema
    hair_growth: ConcernFieldSchema
    fragrance: ConcernFieldSchema
    freshness: ConcernFieldSchema
    
    benefits: List[BenefitSchema] = []
    directions: DirectionsSchema
    skin_type_fit: SkinTypeFitSchema
    hair_type_fit: HairTypeFitSchema
    fragrance_intelligence: FragranceIntelligenceSchema
    
    pregnancy_warning_observation: ReviewObservationSchema
    allergen_warning_observation: ReviewObservationSchema
    sensitivity_warning_observation: ReviewObservationSchema
    
    ingredients_intelligence: List[IngredientIntelligenceSchema] = []

# Product Details output schemas
class FieldValueOut(BaseModel):
    id: uuid.UUID
    field_name: str
    value: Any
    source_type: str
    source_reference: Optional[str] = None
    confidence_score: Optional[float] = None
    review_status: str
    reviewer_id: Optional[uuid.UUID] = None
    enrichment_run_id: Optional[uuid.UUID] = None
    is_current: bool
    created_at: datetime

    class Config:
        from_attributes = True

class VariantOut(BaseModel):
    id: uuid.UUID
    variant_name: Optional[str] = None
    gtin: Optional[str] = None
    size: Optional[str] = None
    unit: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class FormulationOut(BaseModel):
    id: uuid.UUID
    raw_inci_text: str
    market: Optional[str] = None
    language: Optional[str] = None
    effective_date: Optional[date] = None
    created_at: datetime

    class Config:
        from_attributes = True

class ValidationIssueOut(BaseModel):
    id: uuid.UUID
    field_name: Optional[str] = None
    severity: str
    issue_type: str
    message: str
    resolved: bool
    resolved_by_id: Optional[uuid.UUID] = None
    resolved_at: Optional[datetime] = None
    resolution_note: Optional[str] = None
    created_by_type: str
    created_at: datetime

    class Config:
        from_attributes = True

class ProductOut(BaseModel):
    id: uuid.UUID
    product_name: str
    brand_name: str
    category_path: Optional[str] = None
    review_status: str
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class ProductDetailOut(ProductOut):
    brand_id: uuid.UUID
    category_id: Optional[uuid.UUID] = None
    reviewer_id: Optional[uuid.UUID] = None
    variants: List[VariantOut] = []
    formulations: List[FormulationOut] = []
    field_values: List[FieldValueOut] = []
    validation_issues: List[ValidationIssueOut] = []

    class Config:
        from_attributes = True

class ProductEdit(BaseModel):
    field_name: str
    value: Any
    reason: Optional[str] = None

# Ingestion Requests
class IngestProcessRequest(BaseModel):
    filename: str
    file_hash: str
    column_mapping: Dict[str, str]
    save_template: bool = False
    template_name: Optional[str] = None
    source_name: Optional[str] = None
    identical_file_policy: str = "create_new_version"  # reject, resume_previous, create_new_version

# Exports request
class ExportRequest(BaseModel):
    export_mode: str = "business" # business, audit
    file_format: str = "json" # json, csv, xlsx
    include_inferred: bool = False
    webhook_url: Optional[str] = None

class ExportResponse(BaseModel):
    download_url: str
    webhook_triggered: bool = False
