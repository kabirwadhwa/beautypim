# Beauty Product Intelligence Platform (Beauty PIM) MVP

A production-grade PIM dashboard for importing raw retailer product lists, executing Jaro-Winkler fuzzy deduplication matches, running AI-fallback formulations and claims enrichment, tracking full provenance change history, and generating clean business exports.

---

## Technical Architecture

### 1. Canonical Schema & Provenance-First Design
Our database structure separates raw sources from enriched canonical elements, ensuring trace audit trails for compliance:
* **`SourceListing`**: Captures raw feed data without parsing schemas.
* **`CanonicalProduct` & `ProductVariant`**: Standardized clean product identity.shade, size or concentration variants are linked here.
* **`FieldValue`**: Stores individual properties (e.g., `vegan`, `subcategory`). Tracks provenance (`source_type` can be `human_edit`, `ai_inference`, etc.), confidence levels, and active current flags (`is_current = True`).
* **`AuditLog`**: Logs full JSON snapshots (`before_snapshot`, `after_snapshot`, `changed_fields`) for all creation, updates, matching merges, or approvals.

### 2. Multi-Dialect Compatibility
Full structural portability between development (**SQLite**) and production (**PostgreSQL**):
* Hex-encoded UUID strategies with safe sqlite storage.
* Non-blocking conditional unique indices handling dialect-level `sqlite_where` and `postgresql_where` queries.
* System-aware date/time functions handling timezone conversion.

### 3. Deduplication Engine
Fuzzy string Jaro-Winkler evaluations identify identical brand-name pairings, while exact GTIN matches resolve shade/format listing relationships automatically. Resolves match status:
* `exact_match` or `deterministic_match`: Linked immediately.
* `candidate`: Lower similarity threshold, flagged for review.
* `ambiguous`: Multiple close pairings. Halts ingestion progress until reviewed.
* `new_product`: Discovered as unique; creates canonical rows.

---

## Quick Start (Docker Compose)

The easiest way to boot the stack is via Docker Compose, which configures a local Postgres instance, backend server, and client app:

```bash
# 1. Clone or navigate to the directory
cd beauty-pim

# 2. Start all services (Backend, Frontend, PostgreSQL Database)
docker-compose up --build -d

# 3. Access interfaces:
# - Frontend: http://localhost:3000
# - Backend OpenAPI Docs: http://localhost:8000/docs
```

---

## Local Development Setup

If running locally outside Docker, follow these steps:

### Backend Setup
```bash
cd backend

# 1. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start development server
uvicorn app.main:app --reload --port 8000
```

### Frontend Setup
```bash
cd frontend

# 1. Install dependencies
npm install

# 2. Start Next.js development server
npm run dev
```

---

## Testing & Coverage

To run the Pytest suite and generate the 80% coverage metrics:

```bash
cd backend
source venv/bin/activate
PYTHONPATH=. pytest --cov=app tests/
```

---

## Backup and Restore Procedures

### PostgreSQL Production Environment
#### Backup database:
```bash
docker exec -t beauty_pim_db pg_dump -U pim_user -F c beauty_pim > beauty_pim_backup.dump
```

#### Restore database:
```bash
docker exec -i beauty_pim_db pg_restore -U pim_user -d beauty_pim --clean < beauty_pim_backup.dump
```

### SQLite Local Environment
Since SQLite is a single file, backup is a simple copy operation:
```bash
cp backend/beauty_pim.db backend/beauty_pim_backup.db
```

To restore:
```bash
cp backend/beauty_pim_backup.db backend/beauty_pim.db
```

---

## OpenAPI Integration Endpoint Contracts

### Ingestion Feed:
* `POST /api/feeds/upload`: Upload file (multipart), validate format, suggest columns, output preview.
* `POST /api/feeds/process`: Launch background job processing, mapping columns, queue matches.

### Catalog Inspector:
* `GET /api/products`: Search and filter catalog items by status and unresolved warnings.
* `GET /api/products/{id}`: Detailed product properties, validation issues, formulations, and audit log tracking.
* `PUT /api/products/{id}`: Submit manual override, log audit snapshot, trigger warnings refresh.
* `POST /api/products/{id}/approve`: Transition state to Approved. Fails if blocking warning alerts are active.

### Exports Center:
* `POST /api/exports/run`: Generate CSV, JSON, or Excel dataset. Optionally dispatch API payload to a webhook URL.

---

## Internal Knowledge Crawler

BeautyPIM includes an internal, provenance-first crawler for approved beauty
retailer and brand domains. It supports product URLs, URL batches, category and
brand pages, sitemaps and sitemap indexes, and controlled same-domain discovery.
It does not bypass logins, CAPTCHAs, robots rules, or anti-bot controls.

The crawler is intentionally processed outside the API server. Docker Compose
starts `crawler-worker` automatically. When deploying elsewhere, run a second
service from the backend image with this command:

```bash
python -m app.scraping.worker
```

The API and worker must share `DATABASE_URL` and `SECRET_KEY`. Development raw
page evidence is written beneath `CRAWL_RAW_STORAGE_PATH`; configure a persistent
volume for production evidence storage. Browser rendering uses isolated Chromium
and is opt-in per crawl.

The internal UI is available at `/knowledge-crawl` for editors and
administrators. Full-domain crawls require the administrator role.

Key API operations:

* `POST /api/crawl-jobs/validate`
* `POST /api/crawl-jobs`
* `GET /api/crawl-jobs/{id}`
* `POST /api/crawl-jobs/{id}/pause|resume|cancel|recrawl`
* `GET /api/crawl-jobs/{id}/urls|products|conflicts`
* `POST /api/crawl-jobs/{id}/retry-failed`
* `POST /api/crawl-jobs/conflicts/{conflict_id}/accept|reject`

### Importing a structured Retail Data catalogue export

Large Retail Data active-product JSON exports can be streamed directly into the
same provenance, matching, history, and review system without loading the whole
file into memory:

```bash
cd backend
PYTHONPATH=. python scripts/import_retail_dataset.py \
  /path/to/products.active_products_export.json --dry-run

PYTHONPATH=. python scripts/import_retail_dataset.py \
  /path/to/products.active_products_export.json --retain-raw-file
```

The importer is content-hash idempotent by default. Exact identifiers and exact
product matches attach observations to existing products; unmatched items become
reviewable drafts; possible matches and differing values enter review instead of
being silently merged. The exact raw INCI order, field extraction paths, source
URL, source timestamps, normalized values, and file evidence reference are
retained. Use `--force` only when intentionally importing the same file again.

During later enrichment, only catalogue evidence already matched to that exact
canonical product is supplied to the AI. Accepted human and direct-source values
cannot be replaced by an AI inference; disagreements remain non-current
candidates with a review warning.
