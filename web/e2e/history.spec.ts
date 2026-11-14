/**
 * History in a real browser: the story beside the list at desktop width, the arrow keys walking
 * it, and the drawer and no sideways scroll on a phone.
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

test('the newest story opens beside the list, and the arrow keys walk it', async ({ page }) => {
  await start(page);
  const main = page.getByRole('main');
  await expect(main.getByRole('heading', { level: 1, name: 'History' })).toBeVisible();
  const today = main.getByRole('region', { name: 'Today', exact: true });
  const stories = today.getByRole('button', { name: /^What happened: / });
  const beside = main.getByRole('complementary', { name: 'What happened' });

  const first = (await stories.first().textContent()) ?? '';
  await expect(beside.getByRole('article')).toHaveAccessibleName(first);
  // The verdict says what checked means for this kind of work: live, sent, saved, paid, sent in.
  await expect(beside.getByText(/^(It is live|It was sent|The reply is there|All \d+ files|The file is|It was paid|It was sent in|Done|The order|The refund|It ran|It did not|Only part)[^.]*\.$/).first()).toBeVisible();

  await stories.first().focus();
  await page.keyboard.press('ArrowDown');
  const second = (await stories.nth(1).textContent()) ?? '';
  await expect(stories.nth(1)).toBeFocused();
  await expect(beside.getByRole('article')).toHaveAccessibleName(second);
  await expect(page).toHaveURL(/[?&]run=/);
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
