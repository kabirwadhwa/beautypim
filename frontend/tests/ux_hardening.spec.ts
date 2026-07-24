import { test, expect } from '@playwright/test';

test.describe('Beauty PIM UX Hardening E2E Workflows', () => {

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
    await expect(page.locator('h1')).toContainText('Ingestion');
    
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
    
    // Expand details and check for reasoning summary and LLM provider details
    await expect(page.locator('text=Reasoning Summary:')).toBeVisible();

    // 9. Test dynamic concerns targeting cards
    const dynamicConcernsCard = page.locator('text=Dynamic Concern Targeting');
    await expect(dynamicConcernsCard).toBeVisible();

    // 10. Test formulation key ingredients provenance labels
    const ingredientsCard = page.locator('text=Formulation Key Ingredients Provenance');
    await expect(ingredientsCard).toBeVisible();
  });

});
