"""Backfill ai_usage_events from historical enrichment_runs

The usage table starts empty, so every usage panel reads zero until historical
spend is imported. Enrichment runs already carry token counts and the cost
recorded at the time, and they are the only record of past spend that exists.

Doing this as a migration means it happens automatically on deploy. The
alternative was an API call someone had to remember to make, which is not a
feature.

Written as a Python loop rather than INSERT...SELECT because ai_usage_events.id
needs a fresh UUID per row and the GUID type binds differently on PostgreSQL
(native uuid) than on SQLite (CHAR(36)); letting SQLAlchemy bind the values
keeps one code path correct on both.

Revision ID: d8a3f1c6b204
Revises: c7d2e4a81f55
Create Date: 2026-11-11

"""
import uuid

import sqlalchemy as sa
from alembic import op

from app.database import GUID, PortableJSON

revision = "d8a3f1c6b204"
down_revision = "c7d2e4a81f55"
branch_labels = None
depends_on = None

BATCH = 500

# Minimal column definitions, declared locally so this migration keeps working
# even if the application's models move on later.
#

# The source is read through a typed construct rather than raw SQL so that
# SQLAlchemy converts values on the way out: a raw text query returns
# created_at as a string on SQLite, which the typed DateTime column then
# refuses, and ids come back as plain strings instead of UUIDs.
_runs = sa.table(
    "enrichment_runs",
    sa.column("id", GUID()),
    sa.column("provider", sa.String()),
    sa.column("model", sa.String()),
    sa.column("status", sa.String()),
    sa.column("canonical_product_id", GUID()),
    sa.column("product_variant_id", GUID()),
    sa.column("import_job_id", GUID()),
    sa.column("prompt_tokens", sa.Integer()),
    sa.column("completion_tokens", sa.Integer()),
    sa.column("estimated_cost", sa.Numeric(10, 6)),
    sa.column("processing_time_ms", sa.Integer()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)

_usage = sa.table(
    "ai_usage_events",
    sa.column("id", GUID()),
    sa.column("operation", sa.String()),
    sa.column("provider", sa.String()),
    sa.column("model", sa.String()),
    sa.column("status", sa.String()),
    sa.column("canonical_product_id", GUID()),
    sa.column("product_variant_id", GUID()),
    sa.column("import_job_id", GUID()),
    sa.column("enrichment_run_id", GUID()),
    sa.column("prompt_tokens", sa.Integer()),
    sa.column("completion_tokens", sa.Integer()),
    sa.column("cached_prompt_tokens", sa.Integer()),
    sa.column("reasoning_tokens", sa.Integer()),
    sa.column("total_tokens", sa.Integer()),
    sa.column("web_search_calls", sa.Integer()),
    sa.column("estimated_cost_usd", sa.Numeric(12, 6)),
    sa.column("pricing_source", sa.String()),
    sa.column("processing_time_ms", sa.Integer()),
    sa.column("detail", PortableJSON()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()

    # Skip runs already represented, so a re-run imports nothing twice.
    # Compared as strings because SQLite stores CHAR(36) and PostgreSQL uuid.
    existing = {
        str(row[0])
        for row in bind.execute(
            sa.select(_usage.c.enrichment_run_id).where(
                _usage.c.enrichment_run_id.isnot(None)
            )
        )
    }

    rows = bind.execute(
        sa.select(
            _runs.c.id, _runs.c.provider, _runs.c.model, _runs.c.status,
            _runs.c.canonical_product_id, _runs.c.product_variant_id,
            _runs.c.import_job_id, _runs.c.prompt_tokens,
            _runs.c.completion_tokens, _runs.c.estimated_cost,
            _runs.c.processing_time_ms, _runs.c.created_at,
        ).order_by(_runs.c.created_at.asc())
    ).fetchall()

    pending = []
    for row in rows:
        (run_id, provider, model, status, product_id, variant_id, job_id,
         prompt, completion, cost, elapsed, created_at) = row

        if str(run_id) in existing:
            continue

        prompt = int(prompt or 0)
        completion = int(completion or 0)
        # A deterministic-fallback run with no tokens and no cost is not spend.
        if prompt == 0 and completion == 0 and cost is None:
            continue

        pending.append({
            "id": uuid.uuid4(),
            "operation": "enrichment",
            "provider": provider or "unknown",
            "model": model or "unknown",
            "status": "failed" if status == "failed" else "success",
            "canonical_product_id": product_id,
            "product_variant_id": variant_id,
            "import_job_id": job_id,
            "enrichment_run_id": run_id,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_prompt_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": prompt + completion,
            "web_search_calls": 0,
            # Whatever was recorded at the time, not a rate resolved now.
            "estimated_cost_usd": cost,
            "pricing_source": "reference" if cost is not None else "unknown",
            "processing_time_ms": elapsed,
            "detail": {"backfilled_from_enrichment_run": True},
            "created_at": created_at,
        })

        if len(pending) >= BATCH:
            bind.execute(_usage.insert(), pending)
            pending = []

    if pending:
        bind.execute(_usage.insert(), pending)


def downgrade() -> None:
    # Remove only what this migration created, leaving live usage intact.
    op.execute(
        "DELETE FROM ai_usage_events WHERE enrichment_run_id IS NOT NULL "
        "AND operation = 'enrichment'"
    )
