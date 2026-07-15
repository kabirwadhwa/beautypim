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
    is_active: bool
    last_login_at: Optional[datetime] = None
    invited_by_id: Optional[uuid.UUID] = None
    accepted_invitation_at: Optional[datetime] = None
    disabled_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str
    role: str

class TokenData(BaseModel):
    email: Optional[str] = None

# User Invitation Schemas
class UserInvitationCreate(BaseModel):
    email: EmailStr
    role: str

class UserInvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    expires_at: datetime
    last_sent_at: datetime
    resend_count: int
    email_delivery_status: Optional[str] = None
    email_delivery_error: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class UserInvitationValidate(BaseModel):
    token: str

class UserInvitationValidateResponse(BaseModel):
    valid: bool
    email: str
    role: str
    expires_at: datetime

class UserInvitationAccept(BaseModel):
    token: str
    password: str
    password_confirm: str

class AdminUserUpdateRole(BaseModel):
    role: str

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

class FieldEnrichmentMetadataOut(BaseModel):
    enrichment_run_id: Optional[uuid.UUID] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None
    schema_version: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class EnrichmentMetadataSchema(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    schema_version: Optional[str] = None
    status: Optional[str] = None
    tokens: Optional[int] = None
    processing_time_ms: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class KeyIngredientOut(BaseModel):
    name: str
    normalized_inci_name: Optional[str] = None
    functions: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    is_key_ingredient: bool
    key_ingredient_status: Optional[str] = None
    source_type: Optional[str] = None
    evidence: list[Any] = Field(default_factory=list)
    confidence: Optional[float] = None
    formulation_reference: Optional[uuid.UUID] = None

    class Config:
        from_attributes = True

class DynamicConcernOut(BaseModel):
    concern_name: str
    targeting_status: str
    evidence: list[Any] = Field(default_factory=list)
    confidence: Optional[float] = None
    source: str

    class Config:
        from_attributes = True

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
    updated_at: datetime
    
    # Persisted AI Provenance
    override_reason: Optional[str] = None
    evidence: list[Any] = Field(default_factory=list)
    reasoning_summary: Optional[str] = None
    semantic_status: Optional[str] = None
    semantic_status_type: Optional[str] = None
    
    # Per-field metadata
    enrichment_run: Optional[FieldEnrichmentMetadataOut] = None

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
    brand_name: Optional[str] = None
    category_path: Optional[str] = None
    review_status: str
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class ProductDetailOut(ProductOut):
    brand_id: Optional[uuid.UUID] = None
    category_id: Optional[uuid.UUID] = None
    reviewer_id: Optional[uuid.UUID] = None
    variants: list[VariantOut] = Field(default_factory=list)
    formulations: list[FormulationOut] = Field(default_factory=list)
    field_values: list[FieldValueOut] = Field(default_factory=list)
    validation_issues: list[ValidationIssueOut] = Field(default_factory=list)
    
    enrichment_metadata: Optional[EnrichmentMetadataSchema] = None
    key_ingredients: list[KeyIngredientOut] = Field(default_factory=list)
    dynamic_concerns: list[DynamicConcernOut] = Field(default_factory=list)

    class Config:
        from_attributes = True

EDITABLE_FIELDS_REGISTRY = {
    "subcategory": str,
    "product_type": str,
    "gender_target": str,
    "texture": str,
    "application_area": str,
    "target_audience": str,
    "vegan": str,
    "cruelty_free": str,
    "paraben_free": str,
    "sulfate_free": str,
    "silicone_free": str,
    "alcohol_free": str,
    "fragrance_present": str,
    "hydration": bool,
    "anti_ageing": bool,
    "pigmentation": bool,
    "acne": bool,
    "redness": bool,
    "sensitivity": bool,
    "scalp_care": bool,
    "hair_growth": bool,
    "fragrance": bool,
    "freshness": bool
}

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
