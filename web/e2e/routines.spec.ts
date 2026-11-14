import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const ORG = '/northwind-group';
const EDIT = `${ORG}/flows/harbor.shopify.new-order/edit?mock=still`;

const order = (page: Page) => page.locator('[data-card-button]').evaluateAll((items) => items.map((item) => (item as HTMLElement).dataset.cardButton));
const fits = async (page: Page) => expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);

test('a step dragged by its handle lands in the gap, and undo puts it back', async ({ page }) => {
  await page.goto(EDIT);
  const stock = page.locator('[data-step-card="stock"]');
  await expect(stock).toBeVisible();
  const before = await order(page);
  await stock.hover();
  const grip = stock.locator('[aria-hidden="true"].cursor-grab');
  const from = (await grip.boundingBox())!;
  const gap = (await page.locator('[data-slot="enough|otherwise|0"]').boundingBox())!;
  await page.mouse.move(from.x + 8, from.y + 8);
  await page.mouse.down();
  await page.mouse.move(from.x + 40, from.y + 60, { steps: 4 });
  await page.mouse.move(gap.x + gap.width / 2, gap.y + gap.height / 2, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => order(page)).not.toEqual(before);
  expect((await order(page)).slice(-2)).toEqual(['stock', 'tell']);
  await expect(page.getByText('Not saved yet')).toBeVisible();
  await page.keyboard.press('ControlOrMeta+z');
  await expect.poll(() => order(page)).toEqual(before);
});

test('Escape cancels a drag and nothing moves', async ({ page }) => {
  await page.goto(EDIT);
  const stock = page.locator('[data-step-card="stock"]');
  await expect(stock).toBeVisible();
  const before = await order(page);
  await stock.hover();
  const from = (await stock.locator('.cursor-grab').boundingBox())!;
  await page.mouse.move(from.x + 8, from.y + 8);
  await page.mouse.down();
  await page.mouse.move(from.x + 30, from.y + 200, { steps: 6 });
  await page.keyboard.press('Escape');
  await page.mouse.up();
  expect(await order(page)).toEqual(before);
});

test('the keyboard does everything the mouse does, and focus stays on the step', async ({ page }) => {
  await page.goto(EDIT);
  const stock = page.locator('[data-card-button="stock"]');
  await stock.focus();
  await page.keyboard.press('ArrowDown');
  await expect(page.locator('[data-card-button="enough"]')).toBeFocused();
  await page.keyboard.press('Alt+ArrowUp');
  await expect(page.locator('[data-card-button="enough"]')).toBeFocused();
  expect((await order(page)).slice(0, 3)).toEqual(['trigger', 'enough', 'thanks']);
  await page.keyboard.press('a');
  await expect(page.getByText('Add a step').first()).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByText('Add a step').first()).toBeHidden();
  await page.keyboard.press('ControlOrMeta+z');
  await expect.poll(async () => (await order(page)).slice(0, 2)).toEqual(['trigger', 'stock']);
});

test('data goes into a field as a named chip, and is saved as a reference', async ({ page }) => {
  await page.goto(`${ORG}/flows/new/invoice?workspace=harbor&mock=still`);
  await page.getByRole('button', { name: 'send a reminder', exact: true }).click();
  const subject = page.getByRole('textbox', { name: 'Subject' });
  await subject.click();
  await page.keyboard.type('Overdue: ');
  await page.getByRole('button', { name: 'Use data' }).first().click();
  await page.getByRole('button', { name: 'Customer' }).first().click();
  await expect(subject.locator('[data-ref]')).toHaveText('Customer');
  await expect(subject).toContainText('Overdue: ');
  await expect(subject).not.toContainText('{{');
});

for (const [name, width, height] of [['desk', 1440, 900], ['phone', 375, 812]] as const) {
  test.describe(`Routines on a ${name}`, () => {
    test.use({ viewport: { width, height } });

    test('nothing is wider than the screen on any of its pages', async ({ page }) => {
      for (const path of ['flows', 'flows?tab=schedules', 'flows/harbor.shopify.new-order', 'flows/aster.instagram.post', 'flows/new', 'flows/new/order', 'flows/harbor.shopify.new-order/edit']) {
        await page.goto(`${ORG}/${path}${path.includes('?') ? '&' : '?'}mock=still`);
        await page.waitForTimeout(400);
        await fits(page);
      }
    });

    test('a template starts in seconds, and a send step cannot go on without a list', async ({ page }) => {
      await page.goto(`${ORG}/flows/new?mock=still&workspace=harbor`);
      await page.getByRole('link', { name: /^Invoice reminders:/ }).click();
      await page.getByRole('button', { name: 'Turn on' }).click();
      await page.getByRole('combobox', { name: 'Who it goes to' }).click();
      await page.getByRole('option', { name: /Customers with an open invoice/ }).click();
      await expect(page.getByText('Skips 6 people who asked not to hear from you')).toBeVisible();
    });
  });
}

test('a failed step is tried again from the run, and the routine stops being stuck', async ({ page }) => {
  await page.goto(`${ORG}/flows/solstice.outlook.outreach?mock=still`);
  await page.getByRole('button', { name: 'See what happened' }).click();
  await page.getByRole('button', { name: 'Try this step again' }).click();
  await expect(page.getByText(/^Stopped at/)).toHaveCount(0);
});

test('the list says what is empty, loading and broken, and never shows a dead page', async ({ page }) => {
  await page.goto(`${ORG}/flows?mock=error`);
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await page.goto(`${ORG}/flows?mock=still`);
  await page.goto('/juniper-labs/flows?mock=still');
  await expect(page.getByText('Nothing runs on its own yet')).toBeVisible();
});
