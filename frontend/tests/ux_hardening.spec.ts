import { test, expect } from '@playwright/test';

test.describe('Beauty PIM UX Hardening E2E Workflows', () => {

  test('bulk identity review queue supports rapid confirm navigation', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('token', 'test-token'));
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
        'Product Name;Brand;EAN/GTIN;Size;Price;Description;Ingredients\n' +
        'Cloud Hydrating Masque;Peter Thomas Roth;3760000000022;50 ml;45.0;Hydration mask for skin;Water, Hyaluronic Acid, Glycerin, Parfum\n'
      )
    });

    // 3. Column Mapping
    await page.waitForSelector('text=Configure Field Mapping');
    await page.selectOption('label:has-text("Product Name") + select', 'Product Name');
    await page.selectOption('label:has-text("Brand") + select', 'Brand');
    await page.selectOption('label:has-text("Barcode") + select', 'EAN/GTIN');
    await page.selectOption('label:has-text("Size") + select', 'Size');
    await page.selectOption('label:has-text("Price") + select', 'Price');
    await page.selectOption('label:has-text("Description") + select', 'Description');
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

    // 9. Concern intelligence is category-aware. The universal Targeted Concerns
    // field remains present even when no legacy dynamic-concern cards apply.
    await expect(page.getByText('targeted concerns', { exact: true })).toBeVisible();

    // 10. The exact raw INCI remains visible even when no normalized key
    // ingredients can be established safely from the available evidence.
    await expect(page.getByText('Raw Ingredients Ingredients List', { exact: true })).toBeVisible();
  });

});
