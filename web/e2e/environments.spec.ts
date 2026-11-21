/**
 * Environments in a real browser: the list as pictures of each screen, and one environment on the night band
 * with its live screen (noVNC against the mock relay), the control pill, its ⋯ menu, and the phone.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const ORG = '/northwind-group';

async function start(page: Page, path: string, scenario = 'still') {
  await page.goto('/sign-in');
  await page.evaluate((value) => {
    sessionStorage.setItem('agentos.mock.scenario', value);
    localStorage.removeItem('agentos.mock.signed-out');
  }, scenario);
  await page.goto(path);
}

test('the list shows each open one as a picture of its screen, and the working one as a still of it', async ({ page }) => {
  await start(page, `${ORG}/environments`);
  const open = page.getByRole('list', { name: 'Open environments' });
  await expect(open.getByRole('link')).toHaveCount(6);
  const working = open.getByRole('link', { name: /^Black Friday offer page\. Working/ });
  // the node's still of the whole screen, loaded and shown
  await expect(working.locator('img[src*="desk-still"]')).toHaveCSS('opacity', '1');
  await page.getByRole('radio', { name: /Closed/ }).click();
  await expect(page).toHaveURL(/show=closed/);
  await expect(page.getByRole('table', { name: 'Closed environments' }).getByRole('row')).toHaveCount(8);
  await page.goBack();
  await working.click();
  await expect(page).toHaveURL(/\/environments\/e7k2$/);
});

test('one environment opens on the night band with its live screen; T takes over and hands back', async ({ page }) => {
  await start(page, `${ORG}/environments/e7k2`);
  const control = page.getByRole('region', { name: 'Control of E7k2 browser' });
  await expect(control.getByRole('status').filter({ hasText: 'Claude Code has control' })).toBeVisible();
  // the band is drawn behind the top bar, and the top bar takes the night
  await expect(page.locator('.band-night')).toBeVisible();
  await expect(page.locator('header[data-night]')).toBeVisible();
  await expect(page.locator('[data-lane-input] canvas').first()).toBeVisible({ timeout: 10_000 });

  await page.keyboard.press('t');
  await expect(control).toHaveAttribute('data-control', 'human');
  await expect(control.getByRole('status').filter({ hasText: /^You have control since \d\d:\d\d$/ })).toBeVisible();
  await expect(page.getByText('You have control', { exact: true })).toBeVisible();
  // the white ring outside the screen while it is yours
  await expect(page.locator('.stage-screen')).toHaveCSS('outline-style', 'solid');
  await control.getByRole('button', { name: /^Hand back/ }).click();
  await expect(control).toHaveAttribute('data-control', 'agent');
});

test('Stop agent sits in the pill\'s ⋯ menu and asks first', async ({ page }) => {
  await start(page, `${ORG}/environments/e7k2`);
  const control = page.getByRole('region', { name: 'Control of E7k2 browser' });
  await expect(control.getByRole('button', { name: 'Stop agent' })).toHaveCount(0);
  await control.getByRole('button', { name: 'More for this screen' }).click();
  await page.getByRole('menuitem', { name: 'Stop agent' }).click();
  const dialog = page.getByRole('dialog', { name: /Stop the agent on/ });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(control).toHaveAttribute('data-control', 'agent');
});

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test('the screen and the pill fit, the panels follow, and Full window is in the ⋯ menu', async ({ page }) => {
    await start(page, `${ORG}/environments/e7k2`);
    const control = page.getByRole('region', { name: 'Control of E7k2 browser' });
    const pill = control.locator('.control-pill');
    await expect(pill.getByRole('status')).toHaveText('Claude Code has control');
    // nothing is cut: the words fit the pill
    expect(await pill.getByRole('status').evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    await expect(page.getByRole('button', { name: 'Extend' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await control.getByRole('button', { name: 'More for this screen' }).click();
    await page.getByRole('menuitem', { name: 'Full window' }).click();
    await expect(control).toHaveCSS('position', 'fixed');
  });
});
