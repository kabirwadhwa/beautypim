import uuid
from sqlalchemy import (
    Column, String, Integer, Numeric, Boolean, Date, DateTime, 
    ForeignKey, CheckConstraint, Text, Index, text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base, GUID, PortableJSON

class User(Base):
    __tablename__ = 'users'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False) # admin, editor, viewer
    is_active = Column(Boolean, default=True, nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    invited_by_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    accepted_invitation_at = Column(DateTime(timezone=True), nullable=True)
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(role.in_(['admin', 'editor', 'viewer']), name='check_user_role'),
    )

class UserInvitation(Base):
    __tablename__ = 'user_invitations'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    email = Column(String(255), nullable=False, index=True)
    role = Column(String(50), nullable=False) # admin, editor, viewer
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    invited_by_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    status = Column(String(50), nullable=False, default='pending') # pending, accepted, revoked, expired
    expires_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resend_count = Column(Integer, default=0, nullable=False)
    email_delivery_status = Column(String(50), nullable=True) # sent, failed
    email_delivery_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(role.in_(['admin', 'editor', 'viewer']), name='check_invitation_role'),
        CheckConstraint(status.in_(['pending', 'accepted', 'revoked', 'expired']), name='check_invitation_status'),
        Index('uq_invitation_pending_email', 'email', unique=True, 
              sqlite_where=text("status = 'pending'"),
              postgresql_where=text("status = 'pending'")),
    )

class MappingTemplate(Base):
    __tablename__ = 'mapping_templates'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    source_name = Column(String(255), nullable=False)
    file_type = Column(String(50), nullable=False)
    column_mapping = Column(PortableJSON(), nullable=False)
    transformation_rules = Column(PortableJSON(), nullable=True)
    created_by_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

class ImportJob(Base):
    __tablename__ = 'import_jobs'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    filename = Column(String(255), nullable=False)
    file_hash = Column(String(64), unique=False, nullable=False, index=True)
    status = Column(String(50), nullable=False, default='pending') # pending, processing, completed, failed, cancelled
    total_rows = Column(Integer, default=0, nullable=False)
    processed_rows = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, nullable=True)
    column_mapping = Column(PortableJSON(), nullable=False)
    created_by_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(status.in_(['pending', 'processing', 'completed', 'failed', 'cancelled']), name='check_job_status'),
    )

class SourceListing(Base):
    __tablename__ = 'source_listings'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    import_job_id = Column(GUID, ForeignKey('import_jobs.id', ondelete='CASCADE'), nullable=True)
    crawl_job_id = Column(GUID, ForeignKey('crawl_jobs.id', ondelete='CASCADE'), nullable=True)
    canonical_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='SET NULL'), nullable=True)
    product_variant_id = Column(GUID, ForeignKey('product_variants.id', ondelete='SET NULL'), nullable=True)
    raw_data = Column(PortableJSON(), nullable=False)
    source_hash = Column(String(64), nullable=False, index=True) # Non-unique for cross-job matching
    source_url = Column(Text, nullable=True)
    retailer = Column(String(100), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(import_job_id IS NOT NULL AND crawl_job_id IS NULL) OR "
            "(import_job_id IS NULL AND crawl_job_id IS NOT NULL)",
            name="check_source_listing_origin",
        ),
    )

class ImportJobItem(Base):
    __tablename__ = 'import_job_items'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    import_job_id = Column(GUID, ForeignKey('import_jobs.id', ondelete='CASCADE'), nullable=False)
    source_row_number = Column(Integer, nullable=False)
    source_listing_id = Column(GUID, ForeignKey('source_listings.id', ondelete='SET NULL'), nullable=True)
    canonical_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='SET NULL'), nullable=True)
    product_variant_id = Column(GUID, ForeignKey('product_variants.id', ondelete='SET NULL'), nullable=True)
    status = Column(String(50), nullable=False, default='pending') # pending, processing, awaiting_match_review, enriching, completed, failed, skipped, cancelled
    match_status = Column(String(50), nullable=False, default='not_evaluated') # not_evaluated, exact_match, deterministic_match, candidate, ambiguous, new_product, manually_matched, rejected, skipped
    duplicate_score = Column(Numeric(5, 4), default=0.0, nullable=False)
    enrichment_status = Column(String(50), nullable=False, default='not_requested') # not_requested, queued, processing, succeeded, validation_failed, transient_failed, permanent_failed, rate_limited, configuration_blocked, skipped, cancelled
    retry_count = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    failure_code = Column(String(50), nullable=True)
    failure_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(status.in_(['pending', 'processing', 'awaiting_match_review', 'enriching', 'completed', 'failed', 'skipped', 'cancelled']), name='check_item_status'),
        CheckConstraint(match_status.in_(['not_evaluated', 'exact_match', 'deterministic_match', 'candidate', 'ambiguous', 'new_product', 'manually_matched', 'rejected', 'skipped']), name='check_item_match_status'),
        CheckConstraint(enrichment_status.in_(['not_requested', 'queued', 'processing', 'succeeded', 'validation_failed', 'transient_failed', 'permanent_failed', 'rate_limited', 'configuration_blocked', 'skipped', 'cancelled']), name='check_item_enrichment_status'),
        # Job row uniqueness
        CheckConstraint('import_job_id IS NOT NULL AND source_row_number IS NOT NULL', name='check_item_row_ref'),
        Index('idx_import_job_row', 'import_job_id', 'source_row_number', unique=True),
    )

class Brand(Base):
    __tablename__ = 'brands'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    normalized_name = Column(String(255), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class Category(Base):
    __tablename__ = 'categories'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    parent_id = Column(GUID, ForeignKey('categories.id', ondelete='SET NULL'), nullable=True)
    level = Column(Integer, nullable=False, default=0)
    path = Column(String(500), unique=True, nullable=False, index=True) # Materialized path
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class CanonicalProduct(Base):
    __tablename__ = 'canonical_products'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    brand_id = Column(GUID, ForeignKey('brands.id', ondelete='RESTRICT'), nullable=False)
    product_name = Column(String(255), nullable=False)
    normalized_name = Column(String(255), nullable=False, index=True)
    category_id = Column(GUID, ForeignKey('categories.id', ondelete='SET NULL'), nullable=True)
    image_url = Column(Text, nullable=True)
    review_status = Column(String(50), nullable=False, default='imported') 
    # imported, queued, enriching, enrichment_failed, needs_review, in_review, approved, rejected, published, exported, merged
    reviewer_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    brand = relationship("Brand")

    __table_args__ = (
        CheckConstraint(review_status.in_([
            'imported', 'queued', 'enriching', 'enrichment_failed', 'needs_review', 
            'in_review', 'approved', 'rejected', 'published', 'exported', 'merged'
        ]), name='check_product_review_status'),
        Index('idx_brand_id_norm_prod_name', 'brand_id', 'normalized_name'),
    )

class ProductVariant(Base):
    __tablename__ = 'product_variants'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    canonical_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='CASCADE'), nullable=False)
    variant_name = Column(String(255), nullable=True) # e.g. "50ml", "Shade 01"
    gtin = Column(String(50), unique=True, nullable=True, index=True) # GTIN/EAN/UPC
    size = Column(String(100), nullable=True)
    unit = Column(String(50), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("CanonicalProduct")

class SourcePrice(Base):
    __tablename__ = 'source_prices'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    source_listing_id = Column(GUID, ForeignKey('source_listings.id', ondelete='CASCADE'), nullable=True)
    product_variant_id = Column(GUID, ForeignKey('product_variants.id', ondelete='CASCADE'), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(10), nullable=False)
    original_amount = Column(Numeric(10, 2), nullable=True)
    promotional_amount = Column(Numeric(10, 2), nullable=True)
    retailer = Column(String(100), nullable=True)
    country = Column(String(50), nullable=True)
    captured_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class Formulation(Base):
    __tablename__ = 'formulations'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    canonical_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='CASCADE'), nullable=False)
    product_variant_id = Column(GUID, ForeignKey('product_variants.id', ondelete='SET NULL'), nullable=True)
    source_listing_id = Column(GUID, ForeignKey('source_listings.id', ondelete='SET NULL'), nullable=True)
    raw_inci_text = Column(Text, nullable=False)
    market = Column(String(50), nullable=True)
    language = Column(String(10), nullable=True)
    source_reference = Column(String(255), nullable=True)
    effective_date = Column(Date, nullable=True)
    content_hash = Column(String(64), nullable=False, index=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    product = relationship("CanonicalProduct")

class IngredientDefinition(Base):
    __tablename__ = 'ingredient_definitions'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    # CosIng contains a small number of unusually long systematic ingredient
    # names, so these externally sourced values must not be truncated.
    name = Column(Text, nullable=False)
    normalized_name = Column(Text, unique=True, nullable=False, index=True)
    common_name = Column(Text, nullable=True)
    aliases = Column(PortableJSON(), nullable=True) # JSON list of strings
    function = Column(Text, nullable=True)
    benefits = Column(Text, nullable=True)
    possible_concerns = Column(Text, nullable=True)
    source_name = Column(String(255), nullable=True)
    source_url = Column(Text, nullable=True)
    source_record_id = Column(String(255), nullable=True, index=True)
    regulatory_status = Column(String(100), nullable=True)
    cas_number = Column(String(100), nullable=True)
    ec_number = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class FormulationIngredient(Base):
    __tablename__ = 'formulation_ingredients'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    formulation_id = Column(GUID, ForeignKey('formulations.id', ondelete='CASCADE'), nullable=False)
    ingredient_definition_id = Column(GUID, ForeignKey('ingredient_definitions.id', ondelete='SET NULL'), nullable=True)
    raw_inci_name = Column(String(255), nullable=False)
    position = Column(Integer, nullable=False)
    is_key_ingredient = Column(Boolean, default=False, nullable=False)
    evidence_source = Column(String(255), nullable=True)
    confidence_score = Column(Numeric(3, 2), nullable=True)
    evidence = Column(PortableJSON(), nullable=True)
    key_ingredient_status = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(confidence_score >= 0.0, name='check_ingredient_confidence_min'),
        CheckConstraint(confidence_score <= 1.0, name='check_ingredient_confidence_max'),
        Index('uq_formulation_position', 'formulation_id', 'position', unique=True),
    )

class EnrichmentRun(Base):
    __tablename__ = 'enrichment_runs'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    import_job_id = Column(GUID, ForeignKey('import_jobs.id', ondelete='SET NULL'), nullable=True)
    import_job_item_id = Column(GUID, ForeignKey('import_job_items.id', ondelete='SET NULL'), nullable=True)
    source_listing_id = Column(GUID, ForeignKey('source_listings.id', ondelete='SET NULL'), nullable=True)
    canonical_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='SET NULL'), nullable=True)
    product_variant_id = Column(GUID, ForeignKey('product_variants.id', ondelete='SET NULL'), nullable=True)
    parent_enrichment_run_id = Column(GUID, ForeignKey('enrichment_runs.id', ondelete='SET NULL'), nullable=True)
    provider = Column(String(100), nullable=False) # e.g. "Google Gemini"
    model = Column(String(100), nullable=False)
    model_version = Column(String(50), nullable=False)
    prompt_version = Column(String(50), nullable=False)
    schema_version = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, index=True) # success, failed
    error_details = Column(Text, nullable=True)
    processing_time_ms = Column(Integer, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    estimated_cost = Column(Numeric(10, 6), nullable=True)
    requested_fields = Column(PortableJSON(), nullable=True)
    attempt_number = Column(Integer, default=1, nullable=False)
    retry_reason = Column(Text, nullable=True)
    input_content_hash = Column(String(64), nullable=True, index=True)
    raw_response = Column(Text, nullable=True)
    validation_errors = Column(PortableJSON(), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    product = relationship("CanonicalProduct")

    __table_args__ = (
        CheckConstraint(status.in_(['success', 'failed']), name='check_enrichment_status'),
    )

class FieldValue(Base):
    __tablename__ = 'field_values'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    canonical_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='CASCADE'), nullable=True)
    product_variant_id = Column(GUID, ForeignKey('product_variants.id', ondelete='CASCADE'), nullable=True)
    field_name = Column(String(100), nullable=False)
    value = Column(PortableJSON(), nullable=True)
    source_type = Column(String(50), nullable=False) # source_data, deterministic_rule, ai_inference, human_edit
    source_reference = Column(Text, nullable=True)
    confidence_score = Column(Numeric(3, 2), nullable=True)
    review_status = Column(String(50), nullable=False) # confirmed, inferred, unknown, conflicting, not_applicable
    reviewer_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    enrichment_run_id = Column(GUID, ForeignKey('enrichment_runs.id', ondelete='SET NULL'), nullable=True)
    is_current = Column(Boolean, default=True, nullable=False)
    override_reason = Column(Text, nullable=True)
    evidence = Column(PortableJSON(), nullable=True)
    reasoning_summary = Column(Text, nullable=True)
    semantic_status = Column(String(100), nullable=True)
    semantic_status_type = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("CanonicalProduct")

    __table_args__ = (
        CheckConstraint(
            "(canonical_product_id IS NOT NULL AND product_variant_id IS NULL) OR "
            "(canonical_product_id IS NULL AND product_variant_id IS NOT NULL)",
            name="check_field_value_entity_ref"
        ),
        CheckConstraint(source_type.in_(['source_data', 'deterministic_rule', 'ai_inference', 'human_edit']), name='check_field_source_type'),
        CheckConstraint(review_status.in_(['confirmed', 'inferred', 'unknown', 'conflicting', 'not_applicable']), name='check_field_review_status'),
        CheckConstraint(confidence_score >= 0.0, name='check_field_confidence_min'),
        CheckConstraint(confidence_score <= 1.0, name='check_field_confidence_max'),
        Index('idx_field_values_name_current', 'field_name', 'is_current'),
        
        # Enforce at most one current active value per field name on a given entity
        # Note: In SQLite we use unique index with conditional filter, handled in migration & model indexes
        Index('uq_current_val_product', 'canonical_product_id', 'field_name', unique=True, 
              sqlite_where=text('is_current = 1 AND canonical_product_id IS NOT NULL'), 
              postgresql_where=text('is_current = True AND canonical_product_id IS NOT NULL')),
              
        Index('uq_current_val_variant', 'product_variant_id', 'field_name', unique=True, 
              sqlite_where=text('is_current = 1 AND product_variant_id IS NOT NULL'), 
              postgresql_where=text('is_current = True AND product_variant_id IS NOT NULL')),
    )

class ValidationIssue(Base):
    __tablename__ = 'validation_issues'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    
    # Nullable targets
    import_job_id = Column(GUID, ForeignKey('import_jobs.id', ondelete='CASCADE'), nullable=True)
    import_job_item_id = Column(GUID, ForeignKey('import_job_items.id', ondelete='CASCADE'), nullable=True)
    source_listing_id = Column(GUID, ForeignKey('source_listings.id', ondelete='CASCADE'), nullable=True)
    canonical_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='CASCADE'), nullable=True)
    product_variant_id = Column(GUID, ForeignKey('product_variants.id', ondelete='CASCADE'), nullable=True)
    formulation_id = Column(GUID, ForeignKey('formulations.id', ondelete='CASCADE'), nullable=True)
    field_value_id = Column(GUID, ForeignKey('field_values.id', ondelete='CASCADE'), nullable=True)
    
    field_name = Column(String(100), nullable=True)
    severity = Column(String(50), nullable=False) # informational, warning, blocking
    issue_type = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    
    resolved = Column(Boolean, default=False, nullable=False)
    resolved_by_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolution_note = Column(Text, nullable=True)
    
    created_by_type = Column(String(50), nullable=False) # system, rule, ai, user
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("CanonicalProduct")

    __table_args__ = (
        CheckConstraint(severity.in_(['informational', 'warning', 'blocking']), name='check_issue_severity'),
        CheckConstraint(created_by_type.in_(['system', 'rule', 'ai', 'user']), name='check_issue_creator_type'),
        CheckConstraint(
            "(import_job_id IS NOT NULL AND import_job_item_id IS NULL AND source_listing_id IS NULL AND canonical_product_id IS NULL AND product_variant_id IS NULL AND formulation_id IS NULL AND field_value_id IS NULL) OR "
            "(import_job_id IS NULL AND import_job_item_id IS NOT NULL AND source_listing_id IS NULL AND canonical_product_id IS NULL AND product_variant_id IS NULL AND formulation_id IS NULL AND field_value_id IS NULL) OR "
            "(import_job_id IS NULL AND import_job_item_id IS NULL AND source_listing_id IS NOT NULL AND canonical_product_id IS NULL AND product_variant_id IS NULL AND formulation_id IS NULL AND field_value_id IS NULL) OR "
            "(import_job_id IS NULL AND import_job_item_id IS NULL AND source_listing_id IS NULL AND canonical_product_id IS NOT NULL AND product_variant_id IS NULL AND formulation_id IS NULL AND field_value_id IS NULL) OR "
            "(import_job_id IS NULL AND import_job_item_id IS NULL AND source_listing_id IS NULL AND canonical_product_id IS NULL AND product_variant_id IS NOT NULL AND formulation_id IS NULL AND field_value_id IS NULL) OR "
            "(import_job_id IS NULL AND import_job_item_id IS NULL AND source_listing_id IS NULL AND canonical_product_id IS NULL AND product_variant_id IS NULL AND formulation_id IS NOT NULL AND field_value_id IS NULL) OR "
            "(import_job_id IS NULL AND import_job_item_id IS NULL AND source_listing_id IS NULL AND canonical_product_id IS NULL AND product_variant_id IS NULL AND formulation_id IS NULL AND field_value_id IS NOT NULL)",
            name="check_validation_issue_single_target"
        ),
        Index('idx_validation_issues_unresolved', 'canonical_product_id', 
              postgresql_where=text('resolved = False'), 
              sqlite_where=text('resolved = 0')),
    )

class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(GUID, nullable=False) # Direct store (no FK) so audits remain legible after soft/hard deletes
    entity_display_label = Column(String(255), nullable=True)
    
    user_id = Column(GUID, nullable=True) # Direct store
    actor_type = Column(String(50), nullable=False) # user, system, ai, rule
    action = Column(String(50), nullable=False) # create, update, merge, approve, reject
    
    before_snapshot = Column(PortableJSON(), nullable=True)
    after_snapshot = Column(PortableJSON(), nullable=True)
    changed_fields = Column(PortableJSON(), nullable=False)
    
    reason = Column(Text, nullable=True)
    request_id = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(actor_type.in_(['user', 'system', 'ai', 'rule', 'invited_user']), name='check_audit_actor_type'),
        CheckConstraint(action.in_([
            'create', 'update', 'merge', 'approve', 'reject', 'override',
            'invitation_created', 'invitation_resent', 'invitation_revoked', 'invitation_accepted',
            'user_role_changed', 'user_disabled', 'user_enabled'
        ]), name='check_audit_action_type'),
    )

class CanonicalProductMerge(Base):
    __tablename__ = 'canonical_product_merges'
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    source_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='CASCADE'), nullable=False)
    target_product_id = Column(GUID, ForeignKey('canonical_products.id', ondelete='CASCADE'), nullable=False)
    merged_by_id = Column(GUID, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    domain = Column(String(255), nullable=False, index=True)
    starting_urls = Column(PortableJSON(), nullable=False)
    crawl_mode = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default="queued", index=True)
    configuration = Column(PortableJSON(), nullable=False)
    requested_by_id = Column(GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True, index=True)
    pages_discovered = Column(Integer, nullable=False, default=0)
    pages_fetched = Column(Integer, nullable=False, default=0)
    product_pages_found = Column(Integer, nullable=False, default=0)
    products_parsed = Column(Integer, nullable=False, default=0)
    products_persisted = Column(Integer, nullable=False, default=0)
    products_failed = Column(Integer, nullable=False, default=0)
    pages_skipped = Column(Integer, nullable=False, default=0)
    current_queue_size = Column(Integer, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0)
    consecutive_blocks = Column(Integer, nullable=False, default=0)
    error_summary = Column(Text, nullable=True)
    robots_txt = Column(Text, nullable=True)
    robots_fetched_at = Column(DateTime(timezone=True), nullable=True)
    crawler_version = Column(String(50), nullable=False, default="1.0.0")

    __table_args__ = (
        CheckConstraint(crawl_mode.in_([
            "single_url", "multiple_urls", "category", "brand_catalogue",
            "sitemap", "sitemap_index", "full_domain",
        ]), name="check_crawl_mode"),
        CheckConstraint(status.in_([
            "queued", "discovering", "crawling", "parsing",
            "partially_completed", "completed", "paused", "cancelled",
            "blocked", "failed",
        ]), name="check_crawl_status"),
    )


class CrawlUrl(Base):
    __tablename__ = "crawl_urls"
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    crawl_job_id = Column(GUID, ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(Text, nullable=False)
    normalized_url = Column(Text, nullable=False)
    canonical_url = Column(Text, nullable=True)
    parent_url_id = Column(GUID, ForeignKey("crawl_urls.id", ondelete="SET NULL"), nullable=True)
    depth = Column(Integer, nullable=False, default=0)
    state = Column(String(50), nullable=False, default="queued", index=True)
    page_type = Column(String(50), nullable=True)
    classification_reasons = Column(PortableJSON(), nullable=True)
    priority = Column(Integer, nullable=False, default=100)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)
    http_status = Column(Integer, nullable=True)
    content_type = Column(String(255), nullable=True)
    error_reason = Column(Text, nullable=True)
    discovered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(state.in_([
            "queued", "fetching", "visited", "skipped", "failed", "completed",
        ]), name="check_crawl_url_state"),
        CheckConstraint(page_type.in_([
            "product", "category", "pagination", "editorial", "irrelevant",
            "blocked", "unknown", "sitemap",
        ]), name="check_crawl_url_page_type"),
        Index("uq_crawl_job_normalized_url", "crawl_job_id", "normalized_url", unique=True),
        Index("idx_crawl_frontier_claim", "crawl_job_id", "state", "priority", "next_attempt_at"),
    )


class RawPageObservation(Base):
    __tablename__ = "raw_page_observations"
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    crawl_job_id = Column(GUID, ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    crawl_url_id = Column(GUID, ForeignKey("crawl_urls.id", ondelete="CASCADE"), nullable=False, index=True)
    source_url = Column(Text, nullable=False)
    final_url = Column(Text, nullable=False)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    http_status = Column(Integer, nullable=False)
    response_headers = Column(PortableJSON(), nullable=True)
    content_hash = Column(String(64), nullable=False, index=True)
    structured_data_hash = Column(String(64), nullable=True, index=True)
    etag = Column(Text, nullable=True)
    last_modified = Column(Text, nullable=True)
    storage_reference = Column(Text, nullable=True)
    response_size = Column(Integer, nullable=False, default=0)
    parser_version = Column(String(50), nullable=False)
    unchanged_from_id = Column(GUID, ForeignKey("raw_page_observations.id", ondelete="SET NULL"), nullable=True)


class ScrapedProductObservation(Base):
    __tablename__ = "scraped_product_observations"
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    crawl_job_id = Column(GUID, ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    raw_page_id = Column(GUID, ForeignKey("raw_page_observations.id", ondelete="CASCADE"), nullable=False)
    source_listing_id = Column(GUID, ForeignKey("source_listings.id", ondelete="SET NULL"), nullable=True)
    canonical_product_id = Column(GUID, ForeignKey("canonical_products.id", ondelete="SET NULL"), nullable=True, index=True)
    possible_match_product_id = Column(GUID, ForeignKey("canonical_products.id", ondelete="SET NULL"), nullable=True)
    product_variant_id = Column(GUID, ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True)
    source_name = Column(String(255), nullable=False)
    source_domain = Column(String(255), nullable=False, index=True)
    source_url = Column(Text, nullable=False)
    canonical_url = Column(Text, nullable=False)
    locale = Column(String(20), nullable=True)
    country = Column(String(10), nullable=True)
    retailer_product_id = Column(String(255), nullable=True)
    normalized_payload = Column(PortableJSON(), nullable=False)
    identity_hash = Column(String(64), nullable=False, index=True)
    structured_hash = Column(String(64), nullable=False, index=True)
    match_status = Column(String(50), nullable=False)
    changed_fields = Column(PortableJSON(), nullable=True)
    adapter_name = Column(String(100), nullable=False)
    adapter_version = Column(String(50), nullable=False)
    parser_version = Column(String(50), nullable=False)
    scraped_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(match_status.in_([
            "matched", "unmatched", "possible_match", "conflict",
        ]), name="check_scraped_match_status"),
        Index("uq_crawl_product_observation", "crawl_job_id", "canonical_url", "structured_hash", unique=True),
    )


class ScrapedFieldObservation(Base):
    __tablename__ = "scraped_field_observations"
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    scraped_product_id = Column(GUID, ForeignKey("scraped_product_observations.id", ondelete="CASCADE"), nullable=False, index=True)
    field_name = Column(String(100), nullable=False)
    raw_value = Column(PortableJSON(), nullable=True)
    normalized_value = Column(PortableJSON(), nullable=True)
    extraction_path = Column(Text, nullable=True)
    extraction_method = Column(String(100), nullable=False)
    source_domain = Column(String(255), nullable=False)
    source_url = Column(Text, nullable=False)
    observed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    adapter_version = Column(String(50), nullable=False)
    parser_version = Column(String(50), nullable=False)


class CrawlConflict(Base):
    __tablename__ = "crawl_conflicts"
    id = Column(GUID, primary_key=True, default=uuid.uuid4)
    crawl_job_id = Column(GUID, ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    scraped_product_id = Column(GUID, ForeignKey("scraped_product_observations.id", ondelete="CASCADE"), nullable=False)
    canonical_product_id = Column(GUID, ForeignKey("canonical_products.id", ondelete="CASCADE"), nullable=False, index=True)
    field_name = Column(String(100), nullable=False)
    current_value = Column(PortableJSON(), nullable=True)
    observed_value = Column(PortableJSON(), nullable=True)
    status = Column(String(50), nullable=False, default="pending", index=True)
    reviewed_by_id = Column(GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    review_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(status.in_(["pending", "accepted", "rejected"]), name="check_crawl_conflict_status"),
        Index("uq_pending_crawl_conflict", "scraped_product_id", "field_name", unique=True),
    )
