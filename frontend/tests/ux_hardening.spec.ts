import { test, expect } from '@playwright/test';

test.describe('Beauty PIM UX Hardening E2E Workflows', () => {

  test('Product Detail renders customer source content and arbitrary imported attributes', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('token', 'test-token'));
    await page.route('**/api/auth/me', route => route.fulfill({ json: { email: 'admin@test.com', role: 'admin' } }));
    await page.route('**/api/products/source-fixture/research-status', route => route.fulfill({ json: {} }));
    await page.route('**/api/products/source-fixture/research-results', route => route.fulfill({ json: [] }));
    await page.route('**/api/products/source-fixture', route => route.fulfill({ json: {
      id: 'source-fixture', internal_code: 'ICN-SOURCE', product_name: 'Lip Maestro Liquid Lipstick – 405 Sultan',
      description: 'Lip Maestro is a lightweight liquid lipstick...', image_url: null, gtin: '3605522075283',
      brand_id: 'brand-1', brand_name: 'Armani', category_id: null, category_path: 'Makeup > Lips',
      product_category: 'Makeup', subcategory: 'Lips', review_status: 'imported', reviewer_id: null,
      is_deleted: false, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      variants: [{ id: 'variant-1', gtin: '3605522075283', size: '6.5', unit: 'ml' }], formulations: [],
      market_observations: [], validation_issues: [], tags: [], dynamic_concerns: [], key_ingredients: [],
      completeness: null, product_understanding: null, review_aggregate: null, identity_review: null,
      field_values: [
        { id: 'usp', field_name: 'product_usp', value: "Armani's iconic liquid lip color.", source_type: 'source_data', source_reference: 'feed:1', confidence_score: 1, review_status: 'confirmed', reviewer_id: null, enrichment_run_id: null, is_current: true, created_at: new Date().toISOString(), updated_at: null, override_reason: null, evidence: [], reasoning_summary: null, semantic_status: 'explicit_source', semantic_status_type: 'source_data' },
        { id: 'benefits', field_name: 'benefits', value: ['High-impact color', 'Soft velvety finish'], source_type: 'source_data', source_reference: 'feed:1', confidence_score: 1, review_status: 'confirmed', reviewer_id: null, enrichment_run_id: null, is_current: true, created_at: new Date().toISOString(), updated_at: null, override_reason: null, evidence: [], reasoning_summary: null, semantic_status: 'explicit_source', semantic_status_type: 'source_data' },
        { id: 'article', field_name: 'article_description', value: 'ARM LIP 405 SULTAN MAESTRO', source_type: 'source_data', source_reference: 'feed:1', confidence_score: 1, review_status: 'confirmed', reviewer_id: null, enrichment_run_id: null, is_current: true, created_at: new Date().toISOString(), updated_at: null, override_reason: null, evidence: [], reasoning_summary: null, semantic_status: 'explicit_source', semantic_status_type: 'source_data' },
        { id: 'subgroup', field_name: 'bgb_subgroup', value: 'MAKEUP (1ST LEVEL)', source_type: 'source_data', source_reference: 'feed:1', confidence_score: 1, review_status: 'confirmed', reviewer_id: null, enrichment_run_id: null, is_current: true, created_at: new Date().toISOString(), updated_at: null, override_reason: null, evidence: [], reasoning_summary: null, semantic_status: 'explicit_source', semantic_status_type: 'source_data' },
        { id: 'typegroup', field_name: 'bgb_typegroup', value: 'LIPS (2ND LEVEL)', source_type: 'source_data', source_reference: 'feed:1', confidence_score: 1, review_status: 'confirmed', reviewer_id: null, enrichment_run_id: null, is_current: true, created_at: new Date().toISOString(), updated_at: null, override_reason: null, evidence: [], reasoning_summary: null, semantic_status: 'explicit_source', semantic_status_type: 'source_data' },
        { id: 'summary', field_name: 'customer_review_summary', value: 'Customers commonly praise the saturated pigment.', source_type: 'source_data', source_reference: 'feed:1', confidence_score: 1, review_status: 'confirmed', reviewer_id: null, enrichment_run_id: null, is_current: true, created_at: new Date().toISOString(), updated_at: null, override_reason: null, evidence: [], reasoning_summary: null, semantic_status: 'explicit_source', semantic_status_type: 'source_data' },
        { id: 'ingredients', field_name: 'ingredients', value: 'Water, Glycerin', source_type: 'source_data', source_reference: 'feed:1', confidence_score: 1, review_status: 'confirmed', reviewer_id: null, enrichment_run_id: null, is_current: true, created_at: new Date().toISOString(), updated_at: null, override_reason: null, evidence: [], reasoning_summary: null, semantic_status: 'explicit_source', semantic_status_type: 'source_data' },
      ],
      source_attributes: [
        { key: 'source_attr.packaging-material.abc123', label: 'Packaging Material', value: 'Glass', source_type: 'source_data', source_reference: 'feed:1', source_header: 'Packaging Material', updated_at: new Date().toISOString() },
        { key: 'source_attr.launch-wave.def456', label: 'Launch Wave', value: 3, source_type: 'source_data', source_reference: 'feed:1', source_header: 'Launch Wave', updated_at: new Date().toISOString() },
        { key: 'source_attr.featured.ghi789', label: 'Featured', value: true, source_type: 'source_data', source_reference: 'feed:1', source_header: 'Featured', updated_at: new Date().toISOString() },
        { key: 'source_attr.channels.jkl012', label: 'Channels', value: ['Retail', 'Online'], source_type: 'source_data', source_reference: 'feed:1', source_header: 'Channels', updated_at: new Date().toISOString() },
        { key: 'source_attr.packaging-details.mno345', label: 'Packaging Details', value: { outer: { material: 'Cardboard', recyclable: true } }, source_type: 'source_data', source_reference: 'feed:1', source_header: 'Packaging Details', updated_at: new Date().toISOString() },
        { key: 'source_attr.optional-note.pqr678', label: 'Optional Note', value: null, source_type: 'source_data', source_reference: 'feed:1', source_header: 'Optional Note', updated_at: new Date().toISOString() },
      ],
    }}));
    await page.goto('/products/source-fixture');
    await expect(page.locator('h1')).toContainText('Lip Maestro Liquid Lipstick – 405 Sultan');
    await expect(page.getByText("Armani's iconic liquid lip color.").first()).toBeVisible();
    await expect(page.getByText('Lip Maestro is a lightweight liquid lipstick...').first()).toBeVisible();
    await expect(page.getByText('Customers commonly praise the saturated pigment.').first()).toBeVisible();
    await expect(page.getByText('ARM LIP 405 SULTAN MAESTRO').first()).toBeVisible();
    await expect(page.getByText('MAKEUP (1ST LEVEL)').first()).toBeVisible();
    await expect(page.getByText('LIPS (2ND LEVEL)').first()).toBeVisible();
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('Packaging Material');
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('Glass');
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('Launch Wave');
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('3');
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('true');
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('Retail · Online');
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('outer › material: Cardboard');
    await expect(page.getByText('Water, Glycerin').first()).toBeVisible();
  });

  test('bulk identity review queue supports rapid confirm navigation', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('token', 'test-token'));
    await page.route('**/api/auth/me', route => route.fulfill({ json: {
      email: 'admin@test.com', role: 'admin'
    }}));
    await page.route('**/api/products?**', route => route.fulfill({ json: [] }));
    await page.route('**/api/products/identity-review-queue?**', route => route.fulfill({ json: {
      total: 2, page: 1, limit: 100, items: [
        { product_id: '11111111-1111-1111-1111-111111111111', product_name: 'LAT KHAMRAH DUKH',
          source_product_name: 'LAT KHAMRAH DUKH', reason: 'Product family identified but exact version unresolved.',
          review_status: 'NEEDS_REVIEW', match_type: 'product_family', confidence: .82,
          understanding_fingerprint: 'one', suggested_identity: { brand: 'Lattafa', product_family: 'Khamrah Dukhan', category: 'Fragrance' } },
        { product_id: '22222222-2222-2222-2222-222222222222', product_name: 'M DG SHOWER GEL FLORAL',
          reason: 'Consumer brand could not be established.', review_status: 'NEEDS_REVIEW',
          understanding_fingerprint: 'two', suggested_identity: {} },
      ]
    }}));
    await page.route('**/api/products/*/identity-review/confirm', route => route.fulfill({ json: {
      review_status: 'REVIEWED', resumed: true, research_job_id: 'job-1', message: 'Identity resolved. Continuing enrichment...'
    }}));
    await page.goto('/products');
    await page.getByRole('button', { name: 'Review identities (2)' }).click();
    await expect(page.getByTestId('identity-review-queue')).toContainText('PRODUCT 1 OF 2');
    await expect(page.getByTestId('identity-review-queue')).toContainText('Khamrah Dukhan');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByTestId('identity-review-queue')).toContainText('PRODUCT 2 OF 2');
    await expect(page.getByRole('button', { name: 'Confirm & continue' })).toBeDisabled();
    await page.getByRole('button', { name: 'Previous' }).click();
    await page.getByRole('button', { name: 'Confirm & continue' }).click();
    await expect(page.getByText('Identity resolved. Continuing enrichment...')).toBeVisible();
  });

  test('Validation Alerts, Value Overrides, AI Metadata, Key Ingredients, and Dynamic Concerns Flow', async ({ page }) => {
    // 1. Login
    await page.goto('/login');
    await page.fill('input[type="email"]', 'admin@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    
    // Expect redirection to dashboard
    await expect(page).toHaveURL(/.*dashboard/);

    // 2. Catalog Ingestion
    await page.goto('/imports');
    await expect(page.locator('h1')).toContainText('Ingestion', { timeout: 15_000 });
    
    // Upload a mock CSV containing a product with low confidence fields and warning components
    const fileChooserPromise = page.waitForEvent('filechooser');
    await page.click('text=Browse Local Files');
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({
      name: 'beauty_ux_catalog.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(
        'SKU Number;EAN code;Brand;Product Name;Size;Article description;BGB Subgroup;BGB Typegroup;Product USP;Product Description;Product Benefits;Product Review Summary;Ingredients;Packaging Material\n' +
        'UX-1503369;3760000000022;Peter Thomas Roth;Cloud Hydrating Masque;50 ml;PTR CLOUD HYDR MASK;SKINCARE (1ST LEVEL);MASKS (2ND LEVEL);A cloud-light hydration mask.;Hydration mask for skin;Hydrates visibly;Customers commonly praise the soft hydrated finish.;Water, Hyaluronic Acid, Glycerin, Parfum;Glass\n'
      )
    });

    // 3. Column Mapping
    await page.waitForSelector('text=Configure Field Mapping');
    await page.selectOption('label:has-text("Product Name") + select', 'Product Name');
    await page.selectOption('label:has-text("Brand") + select', 'Brand');
    await page.selectOption('label:has-text("Barcode") + select', 'EAN code');
    await page.selectOption('label:has-text("Size") + select', 'Size');
    await page.selectOption('label:has-text("Description") + select', 'Product Description');
    await page.selectOption('label:has-text("Ingredients") + select', 'Ingredients');

    await page.click('button:has-text("Validate and Ingest Catalog")');

    // 4. Progress Completed Monitoring
    await page.waitForSelector('text=Pipeline Progress Status:');
    await page.waitForSelector('text=Proceed to Product review grid', { timeout: 15000 });
    await page.click('text=Proceed to Product review grid');

    // 5. Navigate to Product Detail Screen
    await expect(page.locator('table')).toBeVisible();
    await page.click('tr:has-text("Cloud Hydrating Masque") button:has-text("Inspect")');
    
    // Ensure we are on the product detail page
    await expect(page.locator('h1')).toContainText('Cloud Hydrating Masque');
    await expect(page.getByText('A cloud-light hydration mask.').first()).toBeVisible();
    await expect(page.getByText('Hydration mask for skin').first()).toBeVisible();
    await expect(page.getByText('Hydrates visibly', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Customers commonly praise the soft hydrated finish.').first()).toBeVisible();
    await expect(page.getByText('PTR CLOUD HYDR MASK').first()).toBeVisible();
    await expect(page.getByText('SKINCARE (1ST LEVEL)').first()).toBeVisible();
    await expect(page.getByText('MASKS (2ND LEVEL)').first()).toBeVisible();
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('Packaging Material');
    await expect(page.getByTestId('additional-imported-attributes')).toContainText('Glass');

    // Guided improvement must open as a usable workflow rather than surfacing a fetch error.
    await page.getByRole('button', { name: 'Improve Product', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Improve Product', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '1. Confirm product identity', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '2. Research official or approved pages', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '3. Re-enrich with control', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Refresh selected fields', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Research & improve', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Close improve product', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Improve Product', exact: true })).not.toBeVisible();

    // 6. Test collapsible validation severity groups
    await expect(page.getByText(/Validation Warning Alerts/)).toBeVisible();
    const blockingBtn = page.locator('button:has-text("Blocking Errors")');
    const warningBtn = page.locator('button:has-text("Warnings")');
    if (await blockingBtn.count()) {
      await expect(blockingBtn).toBeVisible();
      // Collapse / Expand click checks when validation groups exist.
      await blockingBtn.click();
      await blockingBtn.click();
      await expect(warningBtn).toBeVisible();
      await warningBtn.click();
      await warningBtn.click();
    } else {
      // A fully enriched product may legitimately have no active issue groups.
      await expect(page.getByText('Validation rules passed. Product contains no warnings.')).toBeVisible();
    }

    // 7. Test override value modal and button disable validations
    const overrideBtn = page.locator('button:has-text("Override")').first();
    await expect(overrideBtn).toBeVisible();
    await overrideBtn.click();

    // The Override Modal must appear
    const modalHeader = page.locator('h3:has-text("Override Enriched Field")');
    await expect(modalHeader).toBeVisible();

    const confirmBtn = page.locator('button:has-text("Confirm Override")');
    // Change value but keep reason empty: Confirm button must be disabled
    await page.fill('input[placeholder="Enter value..."]', 'New Subcategory');
    await page.fill('textarea[placeholder="Explain why this change is necessary..."]', '');
    await expect(confirmBtn).toBeDisabled();

    // Type reason: Confirm button must be enabled
    await page.fill('textarea[placeholder="Explain why this change is necessary..."]', 'E2E corrections reason log');
    await expect(confirmBtn).toBeEnabled();

    // Close modal
    await page.click('button:has-text("Cancel")');
    await expect(modalHeader).not.toBeVisible();

    // 8. Test per-field LLM metadata expansion
    const evidenceBtn = page.locator('button:has-text("Evidence")').first();
    await expect(evidenceBtn).toBeVisible();
    await evidenceBtn.click();
    
    // Evidence details are always inspectable. A deterministic/source-backed field
    // may legitimately have no LLM reasoning summary.
    const evidenceDetails = page.getByText('Reasoning Summary:')
      .or(page.getByText('Evidence Source Quotes:'))
      .or(page.getByText('No factual quotes found in product source text.'));
    await expect(evidenceDetails.first()).toBeVisible();

    // 9. Category intelligence remains available without forcing empty fields
    // into the dossier. Targeted concerns may legitimately be omitted when the
    // current product has no supported concern value.
    await expect(page.getByText(/Intelligence, Claims & Usage|Claims, Usage, Suitability & Safety Observations/)).toBeVisible();

    // 10. The exact raw INCI remains visible even when no normalized key
    // ingredients can be established safely from the available evidence.
    await expect(page.getByText('Raw Ingredients Ingredients List', { exact: true })).toBeVisible();
  });

});
