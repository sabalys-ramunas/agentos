/**
 * The lane window's live view and terminal, in a real browser: noVNC and xterm against the mock
 * relay (src/mocks/streams.ts), which speaks real RFB and answers with the relay's close codes.
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

const screen = (page: Page) => page.getByRole('group', { name: /^(Live view|Terminal) of / });
const toolbar = (page: Page) => screen(page).locator('[data-screen-toolbar]');

/** A fingerprint of the live picture's text field, read from noVNC's canvas. */
function field(page: Page) {
  return page.evaluate(() => {
    const canvas = document.querySelector<HTMLCanvasElement>('[data-lane-input] canvas:not([style*="display: none"])');
    const context = canvas?.getContext('2d');
    if (!canvas || !context) return '';
    // The mock's text field sits at 248,548 on a 960 by 600 screen.
    const data = context.getImageData(Math.round(canvas.width * 0.26), Math.round(canvas.height * 0.915), Math.round(canvas.width * 0.48), Math.round(canvas.height * 0.06)).data;
    let hash = 0;
    for (let index = 0; index < data.length; index += 4) hash = (hash * 31 + data[index]!) >>> 0;
    return String(hash);
  });
}

test('watches read-only, takes over to type into the lane, and hands back', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/instagram`);
  await expect(toolbar(page).getByText('Live', { exact: true })).toBeVisible();
  await expect(toolbar(page).getByText('View only. Take over to type or click')).toBeVisible();
  const before = await field(page);

  // Typing while view only changes nothing on the lane.
  await page.locator('[data-lane-input]').first().click();
  await page.keyboard.type('nope');
  await page.waitForTimeout(400);
  expect(await field(page)).toBe(before);

  const take = page.getByRole('button', { name: /^Take over/ });
  await take.click();
  await expect(page.getByText('You have control')).toBeVisible();
  await expect(toolbar(page).getByText('Your keyboard and mouse go to the lane')).toBeVisible();
  // The keyboard goes into the lane by itself, and g then n stays there.
  await expect(page.getByRole('application').or(page.locator('[data-lane-input] canvas[tabindex="0"]'))).toBeFocused();
  await page.keyboard.type('gn hello');
  await expect.poll(() => field(page)).not.toBe(before);
  await expect(page).toHaveURL(/accounts\/aster\/instagram/);

  // Shift Escape leaves the lane without sending anything to it.
  await page.keyboard.press('Shift+Escape');
  await expect(toolbar(page).getByRole('button').first()).toBeFocused();

  await page.getByRole('button', { name: /^Hand back/ }).click();
  await expect(page.getByText('Agent has control')).toBeVisible();
  await expect(toolbar(page).getByText('View only. Take over to type or click')).toBeVisible();
});

test('hand back puts focus back where it was before the lane took it', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/instagram`);
  await expect(toolbar(page).getByText('Live', { exact: true })).toBeVisible();
  const button = page.getByRole('button', { name: /^Take over/ });
  await button.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[data-lane-input] canvas[tabindex="0"]')).toBeFocused();
  // The agent's idle return, or a hand back from another tab, arrives as a live change.
  await page.getByRole('button', { name: /^Hand back/ }).dispatchEvent('click');
  await expect(page.getByRole('button', { name: /^Take over/ })).toBeFocused();
});

test('says plainly when the node is away, and keeps trying', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/instagram`, 'still,stream-away');
  const notice = screen(page).getByRole('status').filter({ hasText: 'home-mini is away' });
  await expect(notice).toBeVisible();
  await expect(notice).toContainText(/Trying again in \d+ s\./);
  await expect(toolbar(page).getByText('Computer away')).toBeVisible();
});

test('says not allowed, and does not retry', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/instagram`, 'still,stream-denied');
  await expect(screen(page).getByRole('heading', { name: 'Not allowed' })).toBeVisible();
  await expect(screen(page).getByRole('button', { name: /Try/ })).toHaveCount(0);
});

test('says signed out, with the way back in', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/instagram`, 'still,stream-signed-out');
  await expect(screen(page).getByRole('heading', { name: 'You are signed out' })).toBeVisible();
  await expect(screen(page).getByRole('link', { name: 'Sign in' })).toBeVisible();
});

test('reconnects by itself after a drop', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/instagram`, 'still,stream-flaky');
  await expect(toolbar(page).getByText('Live', { exact: true })).toBeVisible();
  const notice = screen(page).getByRole('status').filter({ hasText: 'Reconnecting' });
  await expect(notice).toBeVisible({ timeout: 8_000 });
  await expect(notice).toContainText('The connection dropped.');
  // No click: it comes back on its own.
  await expect(toolbar(page).getByText('Live', { exact: true })).toBeVisible({ timeout: 5_000 });
});

test('the live view ends when the lane is closed, and says so', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/blog`);
  await expect(toolbar(page).getByText('Live', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Put to sleep', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'This account is sleeping' })).toBeVisible();
});

test('the terminal opens on take over, runs a command, and ends with exit', async ({ page }) => {
  await start(page, `${ORG}/lanes/personal/shell`);
  await page.getByRole('button', { name: 'Wake it up' }).click();
  await expect(screen(page).getByRole('heading', { name: 'The terminal is closed' })).toBeVisible({ timeout: 8_000 });
  await page.getByRole('button', { name: /^Take over/ }).click();
  await expect(toolbar(page).getByText('Your typing goes to the shell')).toBeVisible();
  const rows = screen(page).locator('.xterm-rows');
  await expect(rows).toContainText('Type help.');
  await page.keyboard.type('whoami');
  await page.keyboard.press('Enter');
  await expect(rows).toContainText('openajar');
  // Typing in the shell never fires the panel's shortcuts, Ctrl-K included.
  await page.keyboard.press('Control+k');
  await expect(page.getByRole('dialog', { name: 'Search and commands' })).toHaveCount(0);

  await page.getByRole('button', { name: /^Hand back/ }).click();
  await expect(toolbar(page).getByText('Read only. Take over to type')).toBeVisible();
  await page.getByRole('button', { name: /^Take over/ }).click();
  await expect(toolbar(page).getByText('Your typing goes to the shell')).toBeVisible();
  await page.keyboard.type('exit');
  await page.keyboard.press('Enter');
  await expect(screen(page).getByRole('heading', { name: 'The terminal ended' })).toBeVisible();
  await expect(screen(page).getByText('The shell exited.')).toBeVisible();
});

test('a terminal waiting for approval says where to approve it', async ({ page }) => {
  await start(page, `${ORG}/lanes/personal/shell`, 'still,stream-waiting');
  await page.getByRole('button', { name: 'Wake it up' }).click();
  await expect(screen(page)).toBeVisible({ timeout: 8_000 });
  await page.getByRole('button', { name: /^Take over/ }).click();
  await expect(screen(page).getByRole('heading', { name: 'Waiting for approval' })).toBeVisible();
  await expect(screen(page).getByRole('link', { name: 'Open Needs you' })).toBeVisible();
});

test.describe('on a phone', () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

  test('fills the window, zooms, and types a code from the on-screen field', async ({ page }) => {
    await start(page, `${ORG}/lanes/harbor/linkedin`);
    await expect(toolbar(page).getByText('Live', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /^Take over/ }).click();
    await expect(page.getByText('You have control')).toBeVisible();
    await page.getByRole('button', { name: 'Full window' }).click();
    const frame = page.getByRole('region', { name: 'Control of LinkedIn' });
    const box = await frame.boundingBox();
    expect(box?.width).toBe(375);
    expect(box?.height).toBeGreaterThan(700);

    await page.getByRole('button', { name: 'Zoom in' }).click();
    await expect(toolbar(page).getByText('150%')).toBeVisible();
    await page.getByRole('button', { name: 'Fit to the window' }).click();
    await expect(toolbar(page).getByText('100%')).toBeVisible();

    const before = await field(page);
    await page.getByRole('button', { name: 'Type into the screen' }).click();
    await page.getByRole('textbox', { name: 'Text to send to the screen' }).fill('482913');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect.poll(() => field(page)).not.toBe(before);

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await page.getByRole('button', { name: 'Leave full window' }).click();
    await expect(page.getByRole('button', { name: 'Full window' })).toBeFocused();
  });
});
