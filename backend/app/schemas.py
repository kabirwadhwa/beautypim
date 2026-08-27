from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Any, Dict, Literal
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
    display_name: Optional[str] = None
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

class AdminUserUpdateName(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)

class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "admin"
    display_name: Optional[str] = Field(default=None, max_length=120)

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
    source_name: Optional[str] = None
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

class AudienceProfilesFieldSchema(BaseModel):
    value: List[str] = Field(min_length=3, max_length=3)
    value_status: str
    evidence: List[EvidenceItemSchema] = []
    reasoning_summary: str
    confidence: float

class StructuredClaimSchema(BaseModel):
    name: str
    value: Optional[str] = None
    status: Literal["verified", "source_supported", "unverified", "conflicting", "unknown"] = "unknown"
    evidence: List[EvidenceItemSchema] = []
    reasoning_summary: str = ""
    confidence: float = 0.5

class WarningConsiderationSchema(BaseModel):
    type: Literal["allergen", "sensitivity", "regulatory", "pregnancy", "other"]
    observation: str
    evidence: List[EvidenceItemSchema] = []
    source_status: Literal["source_supported", "unverified", "conflicting", "unknown"] = "unknown"
    confidence: float = 0.5

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

class IngredientIntelligenceSchema(BaseModel):
    ingredient_name: str
    inci_position: Optional[int] = None
    short_description: Optional[str] = None
    functions: List[str] = []
    benefits: List[str] = []
    possible_concerns: List[Dict[str, Any]] = []
    is_key_ingredient: bool
    key_ingredient_status: str

class SkincareModuleSchema(BaseModel):
    skin_types: SkinTypeFitSchema
    texture: Optional[CategoricalFieldSchema] = None
    finish: Optional[CategoricalFieldSchema] = None
    key_ingredients: List[IngredientIntelligenceSchema] = []

class HaircareModuleSchema(BaseModel):
    hair_types: HairTypeFitSchema
    texture_format: Optional[CategoricalFieldSchema] = None
    key_ingredients: List[IngredientIntelligenceSchema] = []

class MakeupModuleSchema(BaseModel):
    shade_colour: Optional[CategoricalFieldSchema] = None
    coverage: Optional[CategoricalFieldSchema] = None
    finish: Optional[CategoricalFieldSchema] = None
    texture_format: Optional[CategoricalFieldSchema] = None

class FragranceModuleSchema(BaseModel):
    concentration: Optional[str] = None
    fragrance_family: Optional[str] = None
    top_notes: List[str] = []
    heart_notes: List[str] = []
    base_notes: List[str] = []
    longevity: Optional[str] = None
    sillage_projection: Optional[str] = None
    seasonal_fit: List[str] = []
    occasion_fit: List[str] = []
    evidence: List[EvidenceItemSchema] = []
    confidence: float = 0.5

class StringListFieldSchema(BaseModel):
    values: List[str] = []
    value_status: str = "inferred"
    evidence: List[EvidenceItemSchema] = []
    reasoning_summary: str = ""
    confidence: float = 0.5

class BeautyProductEnrichmentSchema(BaseModel):
    subcategory: CategoricalFieldSchema
    product_type: CategoricalFieldSchema
    application_area: CategoricalFieldSchema
    target_audience: AudienceProfilesFieldSchema
    product_positioning: Optional[CategoricalFieldSchema] = None
    sensory_description: Optional[CategoricalFieldSchema] = None
    routine_time: Optional[CategoricalFieldSchema] = None
    routine_step: Optional[CategoricalFieldSchema] = None
    targeted_concerns: Optional[StringListFieldSchema] = None
    claims: List[StructuredClaimSchema] = []
    benefits: List[BenefitSchema] = []
    directions: DirectionsSchema
    warnings_considerations: List[WarningConsiderationSchema] = []
    skincare: Optional[SkincareModuleSchema] = None
    haircare: Optional[HaircareModuleSchema] = None
    makeup: Optional[MakeupModuleSchema] = None
    fragrance: Optional[FragranceModuleSchema] = None
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
    position: Optional[int] = None
    functions: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    caution_notes: list[str] = Field(default_factory=list)
    is_key_ingredient: bool
    key_ingredient_status: Optional[str] = None
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

class MarketObservationOut(BaseModel):
    source_name: Optional[str] = None
    source_domain: Optional[str] = None
    market: Optional[str] = None
    price: Optional[float] = None
    promotional_price: Optional[float] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    review_summary: Optional[Any] = None
    source_url: Optional[str] = None
    observed_at: Optional[datetime] = None

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
    internal_code: str
    product_name: str
    brand_name: Optional[str] = None
    category_path: Optional[str] = None
    product_category: Optional[str] = None
    subcategory: Optional[str] = None
    product_type: Optional[str] = None
    gtin: Optional[str] = None
    variant_count: int = 0
    image_url: Optional[str] = None
    review_status: str
    validation_issue_count: int = 0
    highest_issue_severity: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    identity_review_status: Optional[str] = None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class SourceAttributeOut(BaseModel):
    key: str
    label: str
    value: Any
    source_type: str
    source_reference: Optional[str] = None
    source_header: str
    updated_at: datetime


class ProductDetailOut(ProductOut):
    brand_id: Optional[uuid.UUID] = None
    category_id: Optional[uuid.UUID] = None
    description: Optional[str] = None
    reviewer_id: Optional[uuid.UUID] = None
    variants: list[VariantOut] = Field(default_factory=list)
    formulations: list[FormulationOut] = Field(default_factory=list)
    field_values: list[FieldValueOut] = Field(default_factory=list)
    source_attributes: list[SourceAttributeOut] = Field(default_factory=list)
    validation_issues: list[ValidationIssueOut] = Field(default_factory=list)
    
    enrichment_metadata: Optional[EnrichmentMetadataSchema] = None
    key_ingredients: list[KeyIngredientOut] = Field(default_factory=list)
    dynamic_concerns: list[DynamicConcernOut] = Field(default_factory=list)
    market_observations: list[MarketObservationOut] = Field(default_factory=list)
    review_aggregate: Optional[dict] = None
    corpus_evidence: Optional[dict] = None
    product_understanding: Optional[dict] = None
    completeness: Optional[dict] = None
    improvement_result: Optional[dict] = None
    identity_review: Optional[dict] = None

    class Config:
        from_attributes = True

class ProductImageUpdate(BaseModel):
    image_url: Optional[str] = None

class ProductTagsUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)

EDITABLE_FIELDS_REGISTRY = {
    "subcategory": str,
    "product_type": str,
    "application_area": str,
    "target_audience": list,
    "product_usp": str,
    "product_positioning": str,
    "sensory_description": str,
    "routine_time": str,
    "routine_step": str,
    "targeted_concerns": dict,
    "claims": list,
    "warnings_considerations": list,
    "skincare": dict,
    "haircare": dict,
    "makeup": dict,
    "fragrance": dict,
    "schema_org": dict,
    "ingredients_intelligence": list,
    "availability": str,
    "rating": float,
    "review_count": int,
    "review_summary": dict,
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
    export_mode: Literal["business", "audit"] = "business"
    file_format: Literal["json", "csv", "xlsx"] = "json"
    include_inferred: bool = False
    webhook_url: Optional[str] = None

class ExportResponse(BaseModel):
    download_url: str
    webhook_triggered: bool = False
    row_count: int = 0

class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: Optional[uuid.UUID] = None

class CategoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: Optional[uuid.UUID] = None
    level: int
    path: str
    product_count: int = 0

class ProductCategoryUpdate(BaseModel):
    category_id: Optional[uuid.UUID] = None

class ProductClassificationUpdate(BaseModel):
    category: str = Field(min_length=1, max_length=255)
    subcategory: str = Field(min_length=1, max_length=255)
