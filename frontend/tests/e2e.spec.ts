import { test, expect } from '@playwright/test';

test.describe('Beauty PIM End-to-End Workflows', () => {

  test('User Login, CSV Upload, Mapping, Review, Edit, and Export Flow', async ({ page }) => {
    // 1. Login
    await page.goto('/login');
    await page.fill('input[type="email"]', 'admin@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    
    // Expect redirection to dashboard
    await expect(page).toHaveURL(/.*dashboard/);

    // 2. CSV Upload
    await page.goto('/imports');
    await expect(page.locator('h1')).toContainText('Catalog Ingestion');
    
    // Upload mock CSV file
    const fileChooserPromise = page.waitForEvent('filechooser');
    await page.click('button:has-text("Upload Catalog")');
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({
      name: 'beauty_catalog.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(
        'Product Name;Brand;EAN/GTIN;Size;Price;Description;Ingredients\n' +
        'Water Drench Hyaluronic Cloud Cream;Peter Thomas Roth;3760000000011;50 ml;52.0;Hydrating cream;Water, Hyaluronic Acid, Glycerin\n' +
        'Retinol Cream;Brand B;;1.7 oz;19.99;Anti aging;Water, Retinol'
      )
    });

    // 3. Column Mapping
    await page.waitForSelector('.column-mapping-container');
    await page.selectOption('select[name="product_name"]', 'Product Name');
    await page.selectOption('select[name="brand"]', 'Brand');
    await page.selectOption('select[name="ean"]', 'EAN/GTIN');
    await page.selectOption('select[name="size"]', 'Size');
    await page.selectOption('select[name="price"]', 'Price');
    await page.selectOption('select[name="description"]', 'Description');
    await page.selectOption('select[name="ingredients"]', 'Ingredients');

    await page.click('button:has-text("Validate & Process Ingestion")');

    // 4. Import Progress Monitoring
    await expect(page.locator('.progress-container')).toBeVisible();
    await page.waitForSelector('.progress-completed', { timeout: 15000 });

    // 5. Candidate Match Review
    await page.goto('/products');
    await expect(page.locator('table')).toBeVisible();
    
    // Click on a candidate product matching review
    await page.click('tr:has-text("Water Drench Hyaluronic Cloud Cream") a');
    await expect(page.locator('.match-review-banner')).toBeVisible();
    await page.click('button:has-text("Confirm Identity Match")');
    await expect(page.locator('.match-status')).toContainText('Matched');

    // 6. Product Approval Blocking (due to validation issue)
    await page.goto('/products');
    await page.click('tr:has-text("Retinol Cream") a');
    
    // Retinol Cream is missing its Brand because Brand B is unregistered or empty.
    // Try to click approve and expect failure toast
    await page.click('button:has-text("Approve Product")');
    await expect(page.locator('.toast-error')).toBeVisible();
    await expect(page.locator('.toast-error')).toContainText('validation issue exists');

    // 7. Human Field Edit
    await page.click('.editable-field-brand');
    await page.fill('.field-editor-input', 'Brand B Cosmetics');
    await page.click('.save-edit-button');
    await expect(page.locator('.field-brand-display')).toContainText('Brand B Cosmetics');

    // Now resolve validation issue and approve
    await page.click('button:has-text("Resolve Issue")');
    await page.click('button:has-text("Approve Product")');
    await expect(page.locator('.review-status-badge')).toContainText('Approved');

    // 8. Business Export
    await page.goto('/exports');
    await page.click('button:has-text("Run Business Export")');
    
    // Check that download URL is presented and triggerable
    const downloadPromise = page.waitForEvent('download');
    await page.click('a:has-text("Download File")');
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toContain('beauty_pim_export_business');
  });

});
