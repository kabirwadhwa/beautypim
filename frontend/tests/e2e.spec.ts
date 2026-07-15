import { test, expect } from '@playwright/test';

test.describe('Beauty PIM End-to-End Workflows', () => {

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
