import { test, expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';

const tokenPath = path.join(__dirname, '../../test_invitation_token.txt');

function getLatestToken(): string {
  // Wait up to 3 seconds for file to be created/updated
  const start = Date.now();
  while (Date.now() - start < 3000) {
    if (fs.existsSync(tokenPath)) {
      const content = fs.readFileSync(tokenPath, 'utf8').trim();
      if (content) return content;
    }
  }
  throw new Error("Invitation token file was not written by the backend.");
}

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

  test('2. Resend invalidation and invitation acceptance flows', async ({ page }) => {
    // Clean old token file if exists
    if (fs.existsSync(tokenPath)) {
      fs.unlinkSync(tokenPath);
    }

    // A. Admin login
    await page.goto('/login');
    await page.fill('input[type="email"]', 'admin@test.com');
    await page.fill('input[type="password"]', 'securepassword123');
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL(/.*dashboard/);
    await page.goto('/settings/team');

    // B. Invite e2e_editor@test.com
    await page.click('button:has-text("Invite User")');
    await page.fill('input[placeholder="colleague@brand.com"]', 'e2e_editor@test.com');
    await page.selectOption('label:has-text("System Role") + select', 'editor');
    await page.click('button:has-text("Send Invitation")');
    
    const pendingRow = page.locator('table').last().locator('tr:has-text("e2e_editor@test.com")');
    await expect(pendingRow).toBeVisible();

    // Read first raw token
    const firstToken = getLatestToken();
    fs.unlinkSync(tokenPath);

    // C. Resend the invitation (invalidates the first token)
    await pendingRow.locator('button:has-text("Resend")').click();
    await page.click('button:has-text("Confirm Action")');
    await page.waitForTimeout(1000);

    // Read second raw token
    const secondToken = getLatestToken();
    expect(firstToken).not.toBe(secondToken);

    // Logout admin
    await page.click('text=Sign Out');

    // D. Try accepting with the first (now invalidated) token
    await page.goto(`/accept-invite?token=${firstToken}`);
    await expect(page.locator('h2:has-text("Invitation Error")')).toBeVisible();

    // E. Try accepting with the second (valid) token
    await page.goto(`/accept-invite?token=${secondToken}`);
    await expect(page.locator('h2')).toContainText('Join Beauty PIM');
    await expect(page.locator('text=Invited Role: editor')).toBeVisible();

    // Input invalid details (password mismatch)
    await page.fill('input[placeholder="Minimum 12 characters"]', 'newsecurepassword123');
    await page.fill('input[placeholder="Confirm password"]', 'mismatchingpassword');
    await page.click('button:has-text("Accept Invitation & Join")');
    await expect(page.locator('text=Passwords do not match')).toBeVisible();

    // Input valid details
    await page.fill('input[placeholder="Confirm password"]', 'newsecurepassword123');
    await page.click('button:has-text("Accept Invitation & Join")');
    await page.waitForURL(/.*login/, { timeout: 5000 });

    // F. Verify login works
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
