import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const ORG = '/northwind-group';
const noOverflow = async (page: Page) =>
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);

test('a person connects Gmail from an address to done, and no page has a password field', async ({ page, context }) => {
  await page.goto(`${ORG}/email/connect?mock=still,computer`);
  await page.getByRole('textbox', { name: 'What is the email address?' }).fill('mia@gmail.example');
  await expect(page.getByText('Looks like Gmail')).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: 'I have my 16 letters' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Google\'s page' })).toHaveAttribute('href', 'https://myaccount.google.com/apppasswords');
  await page.getByRole('button', { name: 'I have my 16 letters' }).click();
  const opened = context.waitForEvent('page');
  await page.getByRole('button', { name: 'Open the form on home-mini' }).click();
  const form = await opened;
  await expect(form.getByText('Served by home-mini')).toBeVisible();
  expect(await form.locator('input').count()).toBe(0);
  await form.getByRole('button', { name: 'pretend to paste the 16 letters' }).click();
  await form.getByRole('button', { name: 'Connect' }).click();
  await expect(form.getByRole('heading', { name: 'All done. You can close this tab.' })).toBeVisible();
  await expect(page.getByText('Connected')).toBeVisible({ timeout: 8_000 });
  expect(await page.locator('input[type="password"]').count()).toBe(0);
  await noOverflow(page);
});

test('a dead end still has one way on: 2-Step Verification, then back to the password', async ({ page }) => {
  await page.goto(`${ORG}/email/connect?address=mia%40gmail.example&stage=password&mock=still,computer`);
  await page.getByRole('button', { name: 'Google says this is not available' }).click();
  await expect(page.getByRole('heading', { name: 'Google needs 2-Step Verification first' })).toBeVisible();
  await page.getByRole('link', { name: 'Turn it on at Google' }).click();
  await page.getByRole('button', { name: 'It is on now, carry on' }).click();
  await expect(page.getByRole('button', { name: 'I have my 16 letters' })).toBeVisible();
});

test('all fifty fit one look, a drag leaves several out, and taking them makes the list', async ({ page }) => {
  await page.goto(`${ORG}/email/leads/marinas?mock=still`);
  const take = page.getByRole('button', { name: 'Take all 46' });
  await expect(take).toBeVisible();
  const last = await page.locator('.sheet-tile[data-never]').last().boundingBox();
  const desk = await take.boundingBox();
  expect((last?.y ?? 9_999) + (last?.height ?? 0)).toBeLessThanOrEqual(desk?.y ?? 0);

  const from = await page.locator('[data-lead="lead-1"]').boundingBox();
  const to = await page.locator('[data-lead="lead-3"]').boundingBox();
  await page.mouse.move((from?.x ?? 0) + 20, (from?.y ?? 0) + 20);
  await page.mouse.down();
  await page.mouse.move((to?.x ?? 0) + 20, (to?.y ?? 0) + 20, { steps: 8 });
  await page.mouse.up();
  await expect(page.getByRole('button', { name: 'Take these 43' })).toBeVisible();

  await page.getByRole('button', { name: 'Take these 43' }).click();
  await expect(page.getByRole('heading', { name: /43 people on this list/ })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'A word before your first list' })).toBeVisible();
  await noOverflow(page);
});

test('the keyboard walks the sheet, and space leaves someone out', async ({ page }) => {
  await page.goto(`${ORG}/email/leads/marinas?mock=still`);
  await page.locator('[data-lead="lead-7"]').focus();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('[data-lead="lead-1"]')).toBeFocused();
  await page.keyboard.press('Space');
  await expect(page.locator('[data-lead="lead-1"]')).toHaveAttribute('aria-pressed', 'false');
});

test('dragging the pace moves people between days, and the note stays gone after Understood', async ({ page }) => {
  await page.goto(`${ORG}/email/lists/marinas?mock=still`);
  await page.getByRole('button', { name: 'Understood' }).click();
  const heading = page.getByRole('heading', { name: /finished by/ });
  const before = await heading.textContent();
  await page.getByRole('slider').fill('5');
  await expect(heading).not.toHaveText(before ?? '');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'What each person gets' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'A word before your first list' })).toHaveCount(0);
});

test('more than ten people is turned toward a list before anything is sent', async ({ page }) => {
  await page.goto(`${ORG}/email/new-mail?mock=still`);
  await page.getByRole('button', { name: 'New email' }).click();
  await page.getByRole('textbox', { name: 'To' }).fill(Array.from({ length: 12 }, (_, index) => `p${index}@a.example`).join(', '));
  await expect(page.getByText(/That is 12 people/)).toBeVisible();
});

test('pointing at a line in the chase tells that person\'s story', async ({ page }) => {
  await page.goto(`${ORG}/email/routines/follow-up?mock=still`);
  await page.locator('.chase-row').first().hover();
  await expect(page.getByText(/replied after|answered the follow-up/)).toBeVisible();
  await noOverflow(page);
});
