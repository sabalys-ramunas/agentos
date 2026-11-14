import { expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

const ROUTINE = '/northwind-group/flows/harbor.xero.invoice-reminders?mock=still';
/** An account's settings are folded so the page reads calmly; the switch lives inside. */
const openSettings = (page: Page) => page.locator('summary', { hasText: 'Settings' }).click();
const rectOf = (locator: Locator) =>
  locator.evaluate((node) => {
    const { x, y, width, height } = node.getBoundingClientRect();
    return [x, y, width, height].map(Math.round).join(',');
  });

for (const [name, width, height] of [['desk', 1440, 900], ['phone', 375, 812]] as const) {
  test.describe(`Ask me first on a ${name}`, () => {
    test.use({ viewport: { width, height } });

    test('a flip moves nothing on the page and keeps the keyboard where it was', async ({ page }) => {
      await page.goto(ROUTINE);
      const panel = page.getByRole('region', { name: 'Ask me first' });
      const control = panel.getByRole('switch');
      await expect(control).not.toBeChecked();
      const before = await rectOf(panel);
      await control.focus();
      await page.keyboard.press('Space');
      await expect(panel.getByRole('heading', { name: 'Asks you first' })).toBeVisible();
      expect(await rectOf(panel)).toBe(before);
      await expect(panel.getByText(/^You turned this on/)).toBeVisible();
      expect(await rectOf(panel)).toBe(before);
      await expect(control).toBeFocused();
    });

    test('an account flips without moving, and keeps its honest line', async ({ page }) => {
      await page.goto('/northwind-group/lanes/aster/gmail?mock=still');
      await openSettings(page);
      const panel = page.getByRole('region', { name: 'Ask me first' });
      const control = panel.getByRole('switch');
      await expect(panel.getByText(/stops posts on sites agentos recognises/)).toBeVisible();
      const size = async () => (await rectOf(panel)).split(',').slice(2).join();
      const before = await size();
      await control.click();
      await expect(panel.getByText(/^You turned this on/)).toBeVisible();
      expect(await size()).toBe(before);
    });

    test('turning it off says what is about to happen, and stopping puts it all back', async ({ page }) => {
      await page.goto('/northwind-group/flows/aster.gmail.newsletter?mock=still');
      const panel = page.getByRole('region', { name: 'Ask me first' });
      const control = panel.getByRole('switch');
      await expect(control).toBeChecked();
      await control.focus();
      await page.keyboard.press('Space');
      const dialog = page.getByRole('dialog', { name: 'Confirm it is you' });
      await expect(dialog.getByText(/It will act without waiting for anyone/)).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(panel.getByRole('heading', { name: 'Asks you first' })).toBeVisible();
      await expect(control).toBeChecked();
      await expect(control).toBeFocused();
    });

    test('nothing is wider than the screen where the switches live', async ({ page }) => {
      for (const path of ['/northwind-group/flows/harbor.xero.invoice-reminders', '/northwind-group/accounts', '/northwind-group/lanes/aster/gmail']) {
        await page.goto(`${path}?mock=still`);
        if (path.includes('/lanes/')) await openSettings(page);
        await expect(page.getByRole('switch', { name: /Ask me first for/ }).first()).toBeAttached();
        expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
      }
    });
  });
}

test('a question a safety check forced says why, in plain words, on the phone too', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/northwind-group/needs-you?mock=still');
  await page.getByRole('button', { name: /^Email.*Aster Coffee/ }).click();
  await expect(page.getByText('A safety check stopped this')).toBeVisible();
  await expect(page.getByText(/nobody here wrote/)).toBeVisible();
});
