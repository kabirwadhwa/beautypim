import { test, expect } from '@playwright/test';

test.describe('Beauty PIM End-to-End Workflows', () => {

  test('Product Grid filters overlapping variants by import provenance and keeps it in the URL', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('token', 'import-filter-token'));
    await page.route('**/api/auth/me', route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@test.com', role: 'admin' }),
    }));
    const jobs = [
      { id: '00000000-0000-4000-8000-00000000000b', filename: 'Import B.xlsx', status: 'completed', created_at: '2026-08-28T10:00:00Z' },
      { id: '00000000-0000-4000-8000-00000000000a', filename: 'Import A.xlsx', status: 'completed', created_at: '2026-08-27T10:00:00Z' },
      { id: '00000000-0000-4000-8000-00000000000c', filename: 'Failed.xlsx', status: 'failed', created_at: '2026-08-29T10:00:00Z' },
    ];
    await page.route('**/api/feeds/jobs', route => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(jobs),
    }));
    const variants = Array.from({ length: 5 }, (_, index) => ({
      id: '00000000-0000-4000-8000-000000000777', product_id: '00000000-0000-4000-8000-000000000777',
      product_variant_id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
      internal_code: 'ICN-777', product_name: `Import Product ${index + 1}`, brand_name: 'Import Brand',
      category_path: 'Makeup > Lips', product_category: 'Makeup', subcategory: 'Lips', product_type: 'Lipstick',
      gtin: String(3600000001000 + index), sku: `SKU-${index + 1}`, variant_name: `Variant ${index + 1}`,
      size: '4', unit: 'g', variant_count: 5, review_status: 'approved', validation_issue_count: 0,
      highest_issue_severity: null, tags: [],
    }));
    await page.route('**/api/products?**', route => {
      const params = new URL(route.request().url()).searchParams;
      const jobId = params.get('import_job_id');
      const indexes = jobId === jobs[1].id ? [0, 1, 2] : jobId === jobs[0].id ? [2, 3, 4] : [0, 1, 2, 3, 4];
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(indexes.map(index => variants[index])),
      });
    });

    await page.goto('/products');
    await expect(page.getByText('Import Product 1')).toBeVisible();
    await expect(page.getByText('Import Product 5')).toBeVisible();
    const importSelect = page.getByLabel('Import / Enrichment File');
    await expect(importSelect.locator('option')).toHaveCount(4); // all, latest, A, B; failed is excluded
    await importSelect.selectOption(jobs[1].id);
    await expect(page).toHaveURL(new RegExp(`import_job=${jobs[1].id}`));
    await expect(page.getByText('Import Product 1')).toBeVisible();
    await expect(page.getByText('Import Product 3')).toBeVisible();
    await expect(page.getByText('Import Product 4')).toHaveCount(0);
    await importSelect.selectOption('latest');
    await expect(page).toHaveURL(/import_job=latest/);
    await expect(page.getByText('Import Product 3')).toBeVisible();
    await expect(page.getByText('Import Product 5')).toBeVisible();
    await expect(page.getByText('Import Product 1')).toHaveCount(0);
  });

  test('Product Grid renders sibling variants as separate selectable rows', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('token', 'variant-grid-token'));
    await page.route('**/api/auth/me', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ email: 'admin@test.com', role: 'admin' }) }));
    const productId = '00000000-0000-4000-8000-000000000777';
    const variants = Array.from({ length: 9 }, (_, index) => ({
      id: productId, product_id: productId,
      product_variant_id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
      internal_code: 'ICN-000777', product_name: 'Nine Shade Lipstick', brand_name: 'Grid Brand',
      category_path: 'Makeup > Lips', product_category: 'Makeup', subcategory: 'Lips',
      product_type: 'Lipstick', gtin: String(3600000000100 + index), sku: `SKU-${index + 1}`,
      variant_name: `Shade ${index + 1}`, size: '4', unit: 'g', variant_count: 9,
      review_status: 'imported', validation_issue_count: 0, highest_issue_severity: null, tags: [],
    }));
    await page.route('**/api/products?**', route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(new URL(route.request().url()).searchParams.get('page') === '1' ? variants : []),
    }));
    await page.goto('/products');
    await expect(page.getByRole('status')).toContainText('Showing 9 product variants');
    await expect(page.getByText('Nine Shade Lipstick')).toHaveCount(9);
    await expect(page.getByText('Shade 9 · 4 g')).toBeVisible();
    await page.getByRole('button', { name: 'Inspect' }).nth(8).click();
    await expect(page).toHaveURL(new RegExp(`/products/${productId}\\?variant=${variants[8].product_variant_id}`));
  });

  test('Product Grid loads every backend page and reports families and variants', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('token', 'pagination-test-token'));
    await page.route('**/api/auth/me', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@test.com', role: 'admin' }),
    }));
    const requestedPages: number[] = [];
    await page.route('**/api/products?**', route => {
      const url = new URL(route.request().url());
      const requestedPage = Number(url.searchParams.get('page') || '1');
      requestedPages.push(requestedPage);
      const start = (requestedPage - 1) * 100;
      const rows = Array.from({ length: requestedPage === 1 ? 100 : requestedPage === 2 ? 1 : 0 }, (_, offset) => {
        const index = start + offset + 1;
        const id = `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
        return {
          id, product_id: id,
          product_variant_id: `10000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
          internal_code: `ICN-${String(index).padStart(12, '0')}`,
          product_name: `Pagination Product ${index}`,
          brand_name: 'Pagination Brand',
          category_path: 'Skincare > Face',
          product_category: 'Skincare',
          subcategory: 'Face',
          product_type: 'Moisturizer',
          gtin: String(3600000000000 + index),
          variant_count: 1,
          review_status: 'imported',
          validation_issue_count: 0,
          highest_issue_severity: null,
        };
      });
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rows) });
    });

    await page.goto('/products');

    await expect(page.getByRole('status')).toContainText('Showing 101 product variants');
    await expect(page.getByText('Pagination Product 101')).toBeVisible();
    expect(requestedPages).toEqual([1, 2]);
  });

  test('Bulk Improve displays durable progress through 100 percent', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('token', 'bulk-progress-token'));
    await page.route('**/api/auth/me', route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@test.com', role: 'admin' }),
    }));
    const products = [1, 2].map(index => ({
      id: `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
      product_id: `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
      product_variant_id: `20000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
      internal_code: `ICN-${index}`, product_name: `Bulk Product ${index}`,
      brand_name: 'Bulk Brand', product_category: 'Skincare', subcategory: 'Face',
      product_type: 'Moisturizer', category_path: 'Skincare > Face', gtin: null,
      variant_count: 1, review_status: 'imported', validation_issue_count: 0,
      highest_issue_severity: null, tags: [],
    }));
    await page.route('**/api/products?**', route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(new URL(route.request().url()).searchParams.get('page') === '1' ? products : []),
    }));
    await page.route('**/api/products/bulk/actions/improve', route => route.fulfill({
      status: 202, contentType: 'application/json',
      body: JSON.stringify({
        queued_count: 2, skipped_count: 0, failed_count: 0,
        items: products.map((product, index) => ({
          product_id: product.id, status: 'queued',
          research_job_id: `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
        })),
      }),
    }));
    let statusChecks = 0;
    await page.route('**/api/products/bulk/actions/improve/status', route => {
      statusChecks += 1;
      const finished = statusChecks > 1;
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          completed_count: finished ? 2 : 1, pending_count: finished ? 0 : 1,
          successful_count: finished ? 2 : 1, failed_count: 0,
          progress_percent: finished ? 100 : 50, all_terminal: finished,
          outcome_counts: { improved: finished ? 2 : 1 },
          items: products.map((product, index) => ({
            product_id: product.id, product_name: product.product_name,
            terminal: finished || index === 0,
            business_outcome: finished || index === 0 ? 'improved' : null,
            before_completeness: 42, after_completeness: finished || index === 0 ? 84 : null,
            sources_ingested: finished || index === 0 ? 2 : 0,
            fields_added: finished || index === 0 ? ['description', 'benefits'] : [],
            fields_still_missing: [], error: null,
          })),
        }),
      });
    });

    await page.goto('/products');
    await page.getByRole('checkbox').first().check();
    await page.getByRole('button', { name: /Improve selected \(2\)/ }).click();
    await expect(page.getByLabel('Bulk progress 50%')).toBeVisible();
    await expect(page.getByLabel('Bulk progress 100%')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('2 / 2 finished')).toBeVisible();
    await expect(page.getByText('2 products improved.')).toBeVisible();
    const completedProgress = page.getByRole('status').filter({ hasText: '2 / 2 finished' });
    await expect(completedProgress.getByText('Bulk Product 1')).toBeVisible();
    await expect(page.getByText('42% → 84%').first()).toBeVisible();
  });

  test('User Login, CSV Ingestion, Mapping, Review, Approval, and Catalog Export Flow', async ({ page }) => {
    // 1. Login
    await page.goto('/login');
    await page.fill('input[type="email"]', 'admin@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    
    // Expect redirection to dashboard
    await expect(page).toHaveURL(/.*dashboard/);

    // 2. Catalog Ingestion
    await page.goto('/imports');
    await expect(page.locator('h1')).toContainText('Ingestion');
    
    // Upload mock CSV file
    const fileChooserPromise = page.waitForEvent('filechooser');
    await page.click('text=Browse Local Files');
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({
      name: 'beauty_catalog.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(
        'Product Name;Brand;EAN/GTIN;Size;Price;Description;Ingredients\n' +
        'Water Drench Hyaluronic Cloud Cream;Peter Thomas Roth;3760000000011;50 ml;52.0;Hydrating cream;Water, Hyaluronic Acid, Glycerin, Parfum\n'
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

    // 5. Inspect and Approve Product
    await expect(page.locator('table')).toBeVisible();
    await page.click('tr:has-text("Water Drench Hyaluronic Cloud Cream") button:has-text("Inspect")');
    
    // Ensure we are on the product detail page
    await expect(page.locator('h1')).toContainText('Water Drench Hyaluronic Cloud Cream');

    // Approve the product (should have no blocking issues)
    await page.click('button:has-text("Approve")');
    
    // Ensure the review status badge updates to APPROVED
    await expect(page.locator('text=APPROVED').first()).toBeVisible();

    // 6. Business Export
    await page.goto('/exports');
    
    // Run and download export file
    const downloadPromise = page.waitForEvent('download');
    await page.click('button:has-text("Generate and Download Catalog")');
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toContain('.json');
  });

});
