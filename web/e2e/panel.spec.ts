/**
 * The flows that only a real browser can prove: the service worker, a real EventSource,
 * focus, keyboard shortcuts, and the layout at phone width.
 */
import { expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

const ORG = '/northwind-group';

async function start(page: Page, path: string, scenario = 'still') {
  await page.goto('/sign-in');
  await page.evaluate((value) => {
    sessionStorage.setItem('agentos.mock.scenario', value);
    localStorage.removeItem('agentos.mock.signed-out');
  }, scenario);
  await page.goto(path);
}

test('Home answers first, and an account opens from its card on Accounts', async ({ page }) => {
  await start(page, ORG);
  const main = page.getByRole('main');
  // One sentence says what runs and what failed; the line under it says what waits for you.
  await expect(main.getByRole('heading', { level: 1 })).toHaveText(/running on \d+ machines?\./);
  await expect(main.getByRole('link', { name: /^\d+ things need you$/ })).toHaveAttribute('href', `${ORG}/needs-you`);
  // The workspace cards moved to Accounts.
  await page.goto(`${ORG}/accounts`);
  const harbor = page.getByRole('main').getByRole('region', { name: 'Harbor Supply' });
  await harbor.getByRole('article', { name: 'Reddit' }).getByRole('link', { name: 'Reddit' }).click();
  await expect(page).toHaveURL(/\/accounts\/harbor\/reddit$/);
  await expect(page.getByText('The site wants a quick human check.').first()).toBeVisible();
});

test('Cmd-K finds an account, and accounts only appear once you search', async ({ page }) => {
  await start(page, ORG);
  await expect(page.getByRole('main').getByRole('heading', { level: 1 })).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  const palette = page.getByRole('dialog', { name: 'Search or run a command' });
  await expect(palette).toBeVisible();
  // With nothing typed: the places most used and a few actions, never the accounts.
  await expect(palette.getByRole('option', { name: /^Home\s*G\s*H$/ })).toBeVisible();
  await expect(palette.getByText('Accounts', { exact: true })).toHaveCount(0);
  await page.keyboard.type('aster/medium');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/accounts\/aster\/medium/);
});

test('g then n, then a keyboard run through Needs you', async ({ page }) => {
  await start(page, ORG, 'still,computer'); // the passkey check for a yes is the computer's
  await expect(page.getByRole('main').getByRole('heading', { level: 1 })).toBeVisible();
  await page.keyboard.press('g');
  await page.keyboard.press('n');
  await expect(page).toHaveURL(/needs-you/);
  await expect(page.getByRole('heading', { name: 'Needs you', level: 1 })).toBeVisible();
  const rail = page.getByRole('navigation', { name: 'Main' }).first();
  const waiting = rail.getByRole('link', { name: /Needs you/ });
  await expect(waiting).toContainText(/\d+/);
  const before = Number((await waiting.textContent())?.match(/\d+/)?.[0]);
  // The payment is first: money is the heaviest. It asks again on the desk too, and the confirm
  // arms after a moment.
  await expect(page.getByRole('heading', { level: 2, name: /^Pay \$40/ })).toBeVisible();
  await page.keyboard.press('a');
  const confirm = page.getByRole('dialog', { name: "Prove it's you" });
  await expect(confirm.getByRole('button', { name: 'Use your passkey' })).toBeEnabled();
  await confirm.getByRole('button', { name: 'Use your passkey' }).click();
  await expect(page.getByRole('button', { name: /^Pay \$40 on Facebook/ })).toHaveCount(0);
  await expect(waiting).toContainText(String(before - 1));
  // Never "Approved": it says what is under way, and offers Undo.
  await expect(page.getByText(/^Paying from Meridian Travel's Facebook in \d s$/)).toBeVisible();
  await expect(page.getByText('Approved')).toHaveCount(0);
});

test('nothing moves under the pointer: a double click on Post it posts once, and the next item waits', async ({ page }) => {
  await start(page, `${ORG}/needs-you`);
  await page.getByRole('button', { name: /^Instagram post, Aster Coffee/ }).click();
  const card = page.getByRole('region', { name: 'Selected item' });
  await expect(card.getByRole('heading', { level: 2 })).toHaveText("Post this photo on Aster Coffee's Instagram?");
  // The list is long, so bring the button into view first: the pointer can only press what it can see.
  await card.getByRole('button', { name: 'Post it' }).scrollIntoViewIfNeeded();
  const box = (await card.getByRole('button', { name: 'Post it' }).boundingBox())!;
  const before = await page.evaluate(async () => (await (await fetch('/api/orgs/northwind-group/approvals')).json()).length as number);
  // Two clicks where the button is, then a third a moment later as the next item arrives.
  await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2);
  await expect(card.getByRole('status')).toHaveText(/Posting to Aster Coffee's Instagram/);
  await page.waitForTimeout(950);
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(card.getByRole('heading', { level: 2 })).toHaveText("Post this on Aster Coffee's LinkedIn?");
  await expect(card.getByRole('button', { name: 'Post it' })).toBeEnabled();
  const after = await page.evaluate(async () => (await (await fetch('/api/orgs/northwind-group/approvals')).json()).length as number);
  expect(after).toBe(before - 1);
  // Undo puts it back, as if nobody had answered.
  await page.getByRole('button', { name: 'Undo' }).click();
  await expect(page.getByText(/^Undone\./)).toBeVisible();
  await expect(page.getByRole('button', { name: /^Instagram post, Aster Coffee/ })).toBeVisible();
});

test('step-up opens from a 403 and the request retries by itself', async ({ page }) => {
  await start(page, `${ORG}/nodes/hetzner-1?tab=lanes`);
  await page.getByRole('button', { name: 'Remove machine' }).click();
  await page.getByLabel(/to confirm/).fill('hetzner-1');
  await page.getByRole('dialog').getByRole('button', { name: 'Remove machine' }).click();
  await expect(page.getByText('Confirm it is you')).toBeVisible();
  await page.getByRole('button', { name: 'Use your passkey' }).click();
  await expect(page).toHaveURL(/\/machines$/);
  await expect(page.getByRole('row', { name: /^hetzner-1/ })).toHaveCount(0);
});

test('a new node arrives over the event stream, with no reload', async ({ page }) => {
  await start(page, `${ORG}/nodes`);
  await page.getByRole('button', { name: 'Add a machine' }).click();
  await page.getByRole('button', { name: 'Get install command' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/curl -fsSL/)).toBeVisible();
  await expect(dialog.getByText('Waiting for the machine')).toBeVisible();
  await expect(dialog.getByText(/is connected/)).toBeVisible({ timeout: 12_000 });
  await dialog.getByRole('button', { name: 'Done' }).click();
  await expect(page.getByRole('table', { name: 'Every machine' }).locator('tbody > tr')).toHaveCount(5);
});

test('a lane warms up live after Open', async ({ page }) => {
  await start(page, `${ORG}/lanes/aster/medium`);
  await page.getByRole('button', { name: 'Wake it up' }).click();
  await expect(page.getByRole('heading', { name: 'Warming up' })).toBeVisible();
  await expect(page.getByRole('group', { name: 'Live view of Medium' }).getByText('Live', { exact: true })).toBeVisible({ timeout: 8_000 });
});

test('live changes keep arriving without a reload', async ({ page }) => {
  await start(page, `${ORG}/nodes`, '');
  const cell = page.getByRole('main').getByRole('meter', { name: 'Memory of hetzner-1' });
  await expect(cell).toBeVisible();
  const first = await cell.getAttribute('aria-valuenow');
  await expect.poll(() => cell.getAttribute('aria-valuenow'), { timeout: 16_000 }).not.toBe(first);
});

test('sign out, then back in through the email link', async ({ page }) => {
  await start(page, ORG);
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to openajar' })).toBeVisible();
  await page.goto(ORG);
  await expect(page).toHaveURL(/sign-in/);
  await page.getByLabel('Email').fill('demo@openajar.local');
  await page.getByRole('button', { name: 'Continue with email' }).click();
  await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
  await page.goto('/auth/verify?token=mock-valid');
  await expect(page.getByRole('main').getByRole('link', { name: /Aster Coffee/ }).first()).toBeVisible();
});

test('the theme toggle overrides the system and survives a reload', async ({ page }) => {
  await start(page, ORG);
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  await page.getByRole('radio', { name: 'Dark' }).click();
  await page.keyboard.press('Escape');
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  // Dawn's dark page is the night navy (#090b16); its cards are one step up.
  expect(await background()).toBe('rgb(9, 11, 22)');
  await page.reload();
  expect(await background()).toBe('rgb(9, 11, 22)');
});

test('a keyboard user gets a focus ring, and a mouse click leaves none', async ({ page }) => {
  await start(page, `${ORG}/flows`);
  const schedules = page.getByRole('link', { name: 'The week ahead' });
  const previous = page.getByRole('link', { name: 'Today' });
  const outline = (element: Locator) => element.evaluate((node) => getComputedStyle(node).outlineStyle);
  await schedules.click();
  await expect(schedules).toBeFocused();
  expect(await outline(schedules)).toBe('none');
  await page.keyboard.press('Shift+Tab');
  await expect(previous).toBeFocused();
  expect(await outline(previous)).toBe('solid');
});

test('a dialog takes focus on its first field and gives it back on close', async ({ page }) => {
  await start(page, `${ORG}/nodes`);
  await page.getByRole('button', { name: 'Add a machine' }).click();
  await expect(page.getByLabel('Name')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Add a machine' })).toBeFocused();
});

test('Cmd-K gives focus back when dismissed', async ({ page }) => {
  await start(page, `${ORG}/nodes`);
  const add = page.getByRole('button', { name: 'Add a machine' });
  await add.focus();
  await page.keyboard.press('ControlOrMeta+k');
  await expect(page.getByRole('dialog', { name: 'Search or run a command' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(add).toBeFocused();
});

test('Machines is one table, a row opens the machine beside it, and the address keeps it', async ({ page }) => {
  await start(page, `${ORG}/machines`);
  const table = page.getByRole('table', { name: 'Every machine' });
  await expect(table.getByRole('row', { name: /^hetzner-1/ })).toBeVisible();
  await table.getByRole('row', { name: /^hetzner-1/ }).click();
  await expect(page).toHaveURL(/\/machines\/hetzner-1$/);
  const panel = page.getByRole('complementary', { name: 'hetzner-1' });
  await expect(panel.getByRole('region', { name: 'Lanes' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page).toHaveURL(/\/machines$/);
});

test('a node page tails its logs live, and reading never moves under you', async ({ page }) => {
  await start(page, `${ORG}/nodes/home-mini?tab=logs`, '');
  const log = page.getByRole('log', { name: /Logs from home-mini/ });
  await expect(log.getByText(/heartbeat sent/).first()).toBeVisible();
  // The top bar has its own Live; this one is the log's.
  await expect(page.getByRole('region', { name: 'Logs' }).getByText('Live', { exact: true })).toBeVisible();

  // Scrolled up, the view holds still and new lines wait behind a pill.
  await log.evaluate((element) => element.scrollTo({ top: 0 }));
  const pill = page.getByRole('button', { name: /new|Jump to latest/ });
  await expect(pill).toBeVisible();
  const first = await log.locator('div').first().textContent();
  await page.waitForTimeout(1500);
  expect(await log.locator('div').first().textContent()).toBe(first);
  await pill.click();
  await expect(pill).toBeHidden();

  // A search filters at once and lands in the URL, so a link shares the same view.
  await page.getByRole('searchbox', { name: 'Search the logs' }).fill('vault_fill');
  await expect(page).toHaveURL(/q=vault_fill/);
  await expect(log.getByText(/\[redacted\]/).first()).toBeVisible();
});

test('a node page tab is in the URL, so it survives a reload', async ({ page }) => {
  await start(page, `${ORG}/nodes/hetzner-1?tab=lanes`);
  await page.getByRole('tab', { name: /Health/ }).click();
  await expect(page).toHaveURL(/tab=health/);
  await page.reload();
  await expect(page.getByRole('tab', { name: /Health/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText(/checks? (pass|fail)|needs? a look/).first()).toBeVisible();
});

test.describe('with reduced motion', () => {
  test.use({ reducedMotion: 'reduce' });

  test('animations and transitions are effectively off', async ({ page }) => {
    await start(page, ORG);
    const button = page.getByRole('button', { name: 'Switch workspace' }).first();
    await expect(button).toBeVisible();
    const duration = await button.evaluate((element) => getComputedStyle(element).transitionDuration);
    expect(duration).toBe('0.001s');
  });
});

test.describe('at 375 wide', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test('approving works one-handed, and nothing overflows', async ({ page }) => {
    await start(page, `${ORG}/approve`);
    const left = page.getByText(/^\d+ to decide$/);
    await expect(left).toBeVisible();
    const count = Number((await left.textContent())?.match(/\d+/)?.[0]);
    const yes = page.getByRole('button', { name: 'Post it' });
    const box = (await yes.boundingBox())!;
    // A double tap: the second lands on the done state, never on the next post.
    await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2);
    await expect(page.getByText(`${count - 1} to decide`)).toBeVisible();
    await expect(page.getByRole('heading', { level: 2 })).toHaveText("Post this on Aster Coffee's LinkedIn?");
    await expect(page.getByText(`${count - 1} to decide`)).toBeVisible();
    // The buttons are in the thumb's reach, at the bottom of the screen.
    expect(box.y + box.height).toBeGreaterThan(812 - 140);
    // Once the next card has armed, a person reading it decides it.
    await expect(page.getByRole('button', { name: "Don't post" })).toBeEnabled();
    await page.getByRole('button', { name: "Don't post" }).click();
    await expect(page.getByText(`${count - 2} to decide`)).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBe(0);
  });

  test('a node page fits the phone on every tab', async ({ page }) => {
    for (const tab of ['lanes', 'schedules', 'logs', 'installed', 'resources', 'connection', 'health']) {
      await start(page, `${ORG}/nodes/home-mini?tab=${tab}`);
      await expect(page.getByRole('tab', { selected: true })).toBeVisible();
      await page.waitForTimeout(300);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, tab).toBe(0);
    }
  });

  test('the rail becomes five tabs and More at the thumb', async ({ page }) => {
    await start(page, ORG);
    const tabs = page.getByRole('navigation', { name: 'Main' }).filter({ visible: true });
    await expect(tabs.getByRole('link')).toHaveCount(5);
    // Environments is at the thumb, and Machines went under More (2026-11-15, F12).
    await tabs.getByRole('link', { name: /Environments/ }).click();
    await expect(page).toHaveURL(/\/environments$/);
    await tabs.getByRole('button', { name: /^More/ }).click();
    await page.getByRole('menuitem', { name: 'Machines' }).click();
    await expect(page).toHaveURL(/\/machines$/);
    await tabs.getByRole('button', { name: /^More/ }).click();
    await page.getByRole('menuitem', { name: 'History' }).click();
    await expect(page).toHaveURL(/\/history$/);
  });

  test('Home fits the phone, with the answer and what waits first', async ({ page }) => {
    await start(page, ORG);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Needs you' }).getByRole('listitem').filter({ visible: true })).toHaveCount(3);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBe(0);
  });
});
