import { expect, test } from '@playwright/test';

const ORG = '/northwind-group';

test('pointing at a value on the page lights its partner in the data, with a line between', async ({ page }) => {
  await page.goto(`${ORG}/site-apis/nordic-beans?mock=still`);
  const sheet = page.locator('[data-sheet] [data-cell="coffee|2"]');
  await sheet.hover();
  await expect(sheet).toHaveAttribute('data-lit', '');
  await expect(page.locator('[data-panel] [data-cell="coffee|2"]')).toHaveAttribute('data-lit', '');
  await expect(page.locator('.split-lit')).toHaveCount(1);
  await page.locator('[data-panel] [data-cell="ships|4"]').hover();
  await expect(page.locator('[data-sheet] [data-cell="ships|4"]')).toHaveAttribute('data-lit', '');
});

test('the keyboard reaches every field, and a dialog gives focus back to what opened it', async ({ page }) => {
  await page.goto(`${ORG}/site-apis/nordic-beans?mock=still`);
  await page.locator('[data-bottom="total"]').focus();
  await expect(page.locator('[data-sheet] [data-cell="total|0"]')).toHaveAttribute('data-lit', '');
  const button = page.getByRole('button', { name: 'New key' });
  await button.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(button).toBeFocused();
});

test('a person can teach a page from start to finish, and nothing overflows', async ({ page }) => {
  await page.goto(`${ORG}/site-apis/new?mock=still`);
  await page.getByRole('button', { name: /Aster Coffee, signed in at/ }).click();
  await page.getByRole('button', { name: 'Look at this page' }).click();
  await expect(page.getByRole('heading', { name: 'I found a table of 42 orders' })).toBeVisible();
  await page.getByRole('button', { name: 'Use this table' }).click();
  await page.getByRole('button', { name: /^Buyer: left out/ }).click();
  await expect(page.getByRole('switch', { name: 'Keep Buyer' })).toBeChecked();
  await page.getByRole('button', { name: 'Try it' }).click();
  await expect(page.getByRole('heading', { name: /It worked: 42 rows/ })).toBeVisible();
  await page.getByRole('button', { name: 'Looks right' }).click();
  await page.getByRole('radio', { name: 'JSON definition' }).click();
  await expect(page.getByText('"buyer"').first()).toBeVisible();
  await page.getByRole('radio', { name: 'Steps' }).click();
  await page.getByRole('button', { name: 'Turn it on' }).click();
  await expect(page.getByRole('heading', { name: 'Open orders is on' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
});
