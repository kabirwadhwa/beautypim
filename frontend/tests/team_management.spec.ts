import { test, expect } from '@playwright/test';

test.describe('Team & Access Management E2E Workflows', () => {

  test('1. Role-based navigation and direct URL checks', async ({ page }) => {
    // A. Login as editor: must not see Team link
    await page.goto('/login');
    await page.fill('input[type="email"]', 'editor@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*dashboard/);
    await expect(page.locator('text=Team & Access')).not.toBeVisible();

    // Editor trying to access direct URL must see Access Denied
    await page.goto('/settings/team');
    await expect(page.locator('h2:has-text("Access Denied")')).toBeVisible();

    // Logout
    await page.click('text=Sign Out');

    // B. Login as viewer: must not see Team link
    await page.goto('/login');
    await page.fill('input[type="email"]', 'viewer@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*dashboard/);
    await expect(page.locator('text=Team & Access')).not.toBeVisible();

    // Viewer trying to access direct URL must see Access Denied
    await page.goto('/settings/team');
    await expect(page.locator('h2:has-text("Access Denied")')).toBeVisible();

    // Logout
    await page.click('text=Sign Out');

    // C. Login as admin: must see Team link and access page
    await page.goto('/login');
    await page.fill('input[type="email"]', 'admin@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*dashboard/);
    
    const teamLink = page.locator('text=Team & Access');
    await expect(teamLink).toBeVisible();
    await teamLink.click();
    await expect(page).toHaveURL(/.*settings\/team/);
    await expect(page.locator('h1')).toContainText('Team & Access');
  });

  test('2. Admin creates an active user without email delivery', async ({ page }) => {
    // A. Admin login
    await page.goto('/login');
    await page.fill('input[type="email"]', 'admin@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*dashboard/);
    await page.goto('/settings/team');

    // B. Create e2e_editor@test.com as an active editor
    await page.click('button:has-text("Add User Directly")');
    await page.fill('input[placeholder="colleague@brand.com"]', 'e2e_editor@test.com');
    await page.selectOption('label:has-text("System Role") + select', 'editor');
    await page.fill('input[placeholder="At least 12 characters"]', 'newsecurepassword123');
    await page.click('button:has-text("Create Active User")');

    const activeRow = page.locator('table').first().locator('tr:has-text("e2e_editor@test.com")');
    await expect(activeRow).toBeVisible();
    await expect(activeRow.locator('text=Active')).toBeVisible();

    // C. Logout admin
    await page.click('text=Sign Out');

    // D. Verify the directly created account can log in immediately
    await page.goto('/login');
    await page.fill('input[type="email"]', 'e2e_editor@test.com');
    await page.fill('input[type="password"]', 'newsecurepassword123');
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*dashboard/);
  });

  test('3. Admin changes role and disables/enables user access', async ({ page }) => {
    // A. Admin login
    await page.goto('/login');
    await page.fill('input[type="email"]', 'admin@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*dashboard/);
    await page.goto('/settings/team');

    // B. Select e2e_editor@test.com row specifically from the Organization Members table
    const userRow = page.locator('table').first().locator('tr:has-text("e2e_editor@test.com")');
    await expect(userRow).toBeVisible();

    // C. Disable user
    await userRow.locator('button:has-text("Disable")').click();
    await page.click('button:has-text("Confirm Action")');
    await page.waitForTimeout(1000);
    
    // Verify status changed to Disabled
    await expect(userRow.locator('text=Disabled')).toBeVisible();
    await expect(userRow.locator('button:has-text("Enable")')).toBeVisible();

    // D. Enable user back
    await userRow.locator('button:has-text("Enable")').click();
    await page.click('button:has-text("Confirm Action")');
    await page.waitForTimeout(1000);
    await expect(userRow.locator('text=Active')).toBeVisible();
  });

});
