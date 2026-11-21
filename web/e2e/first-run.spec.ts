/**
 * First run and the invited approver in a real browser, against the mock: sign-up without a
 * checkbox, the goal first, a computer that turns green over the event stream, a connection, the
 * first request arriving live and decided, an invite, and the approver's small menu.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function start(page: Page, path: string, { signedOut = false } = {}) {
  await page.goto('/sign-in');
  await page.evaluate((out) => {
    sessionStorage.setItem('agentos.mock.scenario', 'still');
    if (out) localStorage.setItem('agentos.mock.signed-out', '1');
    else localStorage.removeItem('agentos.mock.signed-out');
  }, signedOut);
  await page.goto(path);
}

test('sign-up asks for an email and nothing else', async ({ page }) => {
  await start(page, '/sign-up', { signedOut: true });
  await expect(page.getByRole('heading', { name: 'Give your AI a computer of its own' })).toBeVisible();
  await expect(page.getByRole('checkbox')).toHaveCount(0);
  await page.getByLabel('Email').fill('new@example.com');
  await page.getByRole('button', { name: 'Continue with email' }).click();
  await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
});

test('first run: the goal, a computer, accounts, an AI, a real first request, and someone who says yes', async ({ page }) => {
  test.setTimeout(90_000);
  await start(page, '/onboarding');
  await expect(page.getByRole('heading', { name: 'What should your AI do first?' })).toBeVisible();
  await page.getByLabel('Business name').fill('Kiln Bakery');
  await page.getByRole('radio', { name: /Post for a business/ }).click();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page).toHaveURL(/\/onboarding\/kiln-bakery\/computer$/);
  await expect(page.getByText('Coming soon')).toHaveCount(2);
  await page.getByRole('button', { name: /I'm technical/ }).click();
  await expect(page.getByRole('button', { name: 'Copy join token' })).toBeVisible();
  // The mock's node enrolls a few seconds later, and the page hears it live.
  await expect(page.getByRole('status').filter({ hasText: /is ready\./ })).toBeVisible({ timeout: 20_000 });
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Which accounts will it use?' })).toBeVisible();
  await expect(page.getByRole('checkbox', { name: /Instagram/ })).toBeChecked();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Connect your AI' })).toBeVisible();
  await page.getByRole('radio', { name: /Claude Code/ }).click();
  await page.getByRole('button', { name: 'Make a connection for Claude Code' }).click();
  await expect(page.getByRole('button', { name: 'Copy command' })).toBeVisible();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Give it its first job' })).toBeVisible();
  await expect(page.getByText(/Using openajar, write an Instagram post for Kiln Bakery/)).toBeVisible();
  const card = page.getByRole('main').getByRole('article').first();
  await expect(card).toBeVisible({ timeout: 20_000 });
  await card.getByRole('button').last().click();
  await expect(page.getByText(/It goes ahead now|Nothing happened/)).toBeVisible();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Stay in the loop, from anywhere' })).toBeVisible();
  await page.getByLabel('Their email').fill('priya@example.com');
  await page.getByRole('button', { name: 'Send invite' }).click();
  await expect(page.getByText('Invite sent to priya@example.com')).toBeVisible();
  await page.getByRole('button', { name: 'Finish and go to Needs you' }).click();
  await expect(page).toHaveURL(/\/kiln-bakery\/needs-you$/);
});

test('an invited approver joins in one click, learns what they will be asked, and gets a small menu', async ({ page }) => {
  await start(page, '/invite/priya-token', { signedOut: true });
  await expect(page.getByRole('heading', { name: 'Mara asked you to help with Aster Coffee' })).toBeVisible();
  await page.getByRole('button', { name: 'Join Aster Coffee' }).click();
  await expect(page).toHaveURL(/\/northwind-group\/welcome/);
  await expect(page.getByText('Can this go out?')).toBeVisible();
  await page.getByRole('link', { name: /what's waiting|Go to Needs you/ }).click();
  await expect(page).toHaveURL(/\/northwind-group\/needs-you/);
  const rail = page.getByRole('navigation', { name: 'Main' }).first();
  await expect(rail.getByRole('link')).toHaveText([/Needs you/, /History/, /Settings/]);
});

test.describe('at 375 wide', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test('an approver has three tabs and no More', async ({ page }) => {
    await start(page, '/invite/priya-token', { signedOut: true });
    await page.getByRole('button', { name: 'Join Aster Coffee' }).click();
    await page.getByRole('link', { name: /what's waiting|Go to Needs you/ }).click();
    const tabs = page.getByRole('navigation', { name: 'Main' }).last();
    await expect(tabs.getByRole('link')).toHaveText([/Needs you/, /History/, /Settings/]);
    await expect(tabs.getByRole('button', { name: /More/ })).toHaveCount(0);
  });

  test('the first job fits the phone', async ({ page }) => {
    await start(page, '/onboarding');
    await expect(page.getByRole('heading', { name: 'What should your AI do first?' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
  });
});
