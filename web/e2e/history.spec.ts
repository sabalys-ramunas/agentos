/**
 * History in a real browser: a story opens in the side sheet at every width, Esc gives focus back,
 * and nothing scrolls sideways on a phone.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const HISTORY = '/northwind-group/activity';

async function start(page: Page) {
  await page.goto('/sign-in');
  await page.evaluate(() => {
    sessionStorage.setItem('agentos.mock.scenario', 'still');
    localStorage.removeItem('agentos.mock.signed-out');
  });
  await page.goto(HISTORY);
}

test('a story opens in the side sheet, the list keeps its width, and Esc gives focus back to the row', async ({ page }) => {
  await start(page);
  const main = page.getByRole('main');
  await expect(main.getByRole('heading', { level: 1, name: 'History' })).toBeVisible();
  const today = main.getByRole('region', { name: 'Today', exact: true });
  const stories = today.getByRole('button', { name: /^What happened: / });
  await expect(main.getByRole('complementary', { name: 'What happened' })).toHaveCount(0);

  const first = (await stories.first().textContent()) ?? '';
  await stories.first().click();
  const sheet = page.getByRole('dialog', { name: 'What happened' });
  await expect(sheet.getByRole('article')).toHaveAccessibleName(first);
  // The verdict says what checked means for this kind of work: live, sent, saved, paid, sent in.
  await expect(sheet.getByText(/^(It is live|It was sent|The reply is there|All \d+ files|The file is|It was paid|It was sent in|Done|The order|The refund|It ran|It did not|Only part)[^.]*\.$/).first()).toBeVisible();
  await expect(page).toHaveURL(/[?&]run=/);
  await page.keyboard.press('Escape');
  await expect(sheet).toBeHidden();
  await expect(stories.first()).toBeFocused();
});

test('on a phone the story opens in a drawer, and nothing scrolls sideways', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await start(page);
  const today = page.getByRole('main').getByRole('region', { name: 'Today', exact: true });
  await expect(today).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBe(0);
  await today.getByRole('button', { name: /^What happened: / }).first().click();
  const drawer = page.getByRole('dialog', { name: 'What happened' });
  await expect(drawer.getByRole('article')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
});
