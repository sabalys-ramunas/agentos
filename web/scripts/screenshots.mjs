#!/usr/bin/env node
/**
 * Screenshots every route at 1440 and 375 wide, in light and dark, into screenshots/.
 * Uses system Chrome, so no browser download.
 *
 *   node scripts/screenshots.mjs                       every route, every variant
 *   node scripts/screenshots.mjs --only home,needs     routes whose name contains a term
 *   node scripts/screenshots.mjs --width 1440 --theme light
 *   node scripts/screenshots.mjs --sheet               also builds contact sheets
 */
/* global window, PushManager, Notification -- used inside page.evaluate, which runs in the browser */
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const out = join(root, 'screenshots');
const base = process.env.BASE_URL ?? 'http://localhost:5310';
const O = 'northwind-group';

const args = process.argv.slice(2);
const option = (name) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 ? args[index + 1] : undefined;
};
const only = option('only')?.split(',');
const widths = option('width') ? [Number(option('width'))] : [1440, 375];
const themes = option('theme') ? [option('theme')] : ['light', 'dark'];
const sheets = args.includes('--sheet');

/** Turns on one person's Technical details. On a phone the switch sits behind More. */
async function technicalOn(page) {
  await page.evaluate(async () => {
    const session = await (await fetch('/api/session')).json();
    localStorage.setItem(`agentos.technical.${session.user.id}`, 'on');
  });
  await page.reload({ waitUntil: 'load' });
  await page.waitForTimeout(600);
}

/** name, path, optional page action, optional mock scenario. */
const routes = [
  ['auth-sign-in', '/sign-in', null, 'signed-out'],
  ['shared-link-invalid', '/shared#token=shr_mock_missing', null, 'signed-out'],
  ['auth-sign-up', '/sign-up', null, 'signed-out'],
  ['auth-check-email', '/check-email?email=demo%40agentos.local', null, 'signed-out'],
  ['auth-verify-expired', '/auth/verify?token=expired', null, 'signed-out'],
  ['auth-verify-step-up', '/auth/verify?token=mock&step_up=1'],
  ['auth-verify-step-up-elsewhere', '/auth/verify?token=mock&step_up=1', null, 'signed-out'],
  ['auth-sign-in-expired', '/sign-in?reason=expired&next=%2Fnorthwind-group%2Fmachines', null, 'signed-out'],
  ['auth-sign-in-google-error', '/sign-in?error=email_unverified', null, 'signed-out'],
  ['legal-aup', '/legal/acceptable-use', null, 'signed-out'],
  ['auth-invite', '/invite/mock-token'],
  ['auth-invite-expired', '/invite/expired', null, 'signed-out'],
  ['auth-invite-used', '/invite/used', null, 'signed-out'],
  ['onboarding-org', '/onboarding'],
  ['onboarding-node', `/onboarding/${O}/node`],
  ['onboarding-template', `/onboarding/${O}/template`],
  ['onboarding-agent', `/onboarding/${O}/agent`],
  ['onboarding-phone', `/onboarding/${O}/phone`],
  ['proof', `/${O}/activity/proof`],
  ['business-queue', `/${O}/workspaces/aster`],
  ['yes-setting', `/${O}/settings/yes`],
  ['yes-places', `/${O}/settings/say-yes`],
  ['yes-in-chat', `/${O}/concept/yes-in-chat`],
  ['bar-email', `/${O}/email`],
  ['bar-site-apis', `/${O}/site-apis`],
  // Workers and AI models: concept stage, on the mock only.
  ['workers', `/${O}/settings/workers`],
  ['workers-technical', `/${O}/settings/workers`, async (page) => {
    await technicalOn(page);
    await page.getByText('AI usage today').first().waitFor();
  }],
  ['workers-new', `/${O}/settings/workers`, null, 'workers-new'],
  ['workers-ready', `/${O}/settings/workers`, null, 'workers-ready'],
  ['workers-turn-on', `/${O}/settings/workers`, async (page) => {
    await page.getByRole('button', { name: 'Turn it on' }).first().click();
    await page.getByRole('dialog').waitFor();
  }, 'workers-ready'],
  ['workers-asked', `/${O}/settings/workers`, null, 'workers-asked'],
  ['workers-robot', `/${O}/settings/workers`, null, 'workers-robot'],
  ['workers-offline', `/${O}/settings/workers`, null, 'workers-offline'],
  ['workers-limit', `/${O}/settings/workers`, null, 'workers-limit'],
  ['workers-loading', `/${O}/settings/workers`, null, 'loading'],
  ['workers-error', `/${O}/settings/workers`, null, 'error'],
  ['workers-empty', '/juniper-labs/settings/workers'],
  ['workers-computer', `/${O}/machines/home-mini?tab=workers`],
  ['workers-computer-none', `/${O}/machines/hetzner-1?tab=workers`],
  ['workers-needs-you', `/${O}/needs-you`, async (page) => {
    await page.getByText(/Two orders are called/).first().click();
    await page.getByRole('slider').filter({ visible: true }).first().waitFor();
  }],
  ['workers-history', `/${O}/history`, async (page) => {
    const rows = page.getByRole('button', { name: /^What happened: / });
    const count = await rows.count();
    for (let index = 0; index < count; index += 1) {
      await rows.nth(index).click();
      await page.waitForTimeout(120);
      if (await page.getByRole('slider').filter({ visible: true }).count()) break;
      if (await page.getByRole('dialog').count()) await page.keyboard.press('Escape');
      await page.waitForTimeout(200);
    }
  }],
  ['workers-account', `/${O}/accounts/aster/instagram`],
  ['workers-account-off', `/${O}/accounts/aster/linkedin`],
  ['ai-models', `/${O}/settings/brains`],
  ['ai-models-technical', `/${O}/settings/brains`, async (page) => {
    await technicalOn(page);
    await page.getByText(/Your agents try these/).waitFor();
  }],
  ['ai-models-empty', '/juniper-labs/settings/brains'],
  ['ai-models-loading', `/${O}/settings/brains`, null, 'loading'],
  ['ai-models-add', `/${O}/settings/brains/add`],
  ['ai-models-add-typesafe', `/${O}/settings/brains/add?service=typesafe&computer=home-mini`],
  ['ai-models-add-terminal', `/${O}/settings/brains/add?service=typesafe&computer=hetzner-1`],
  ['ai-key-form', '/computer-form/ai-key?service=typesafe&computer=home-mini', null, 'signed-out'],
  ['home', `/${O}`],
  ['home-all-good', `/${O}`, null, 'all-good'],
  ['accounts', `/${O}/accounts`],
  ['accounts-needs', `/${O}/accounts`, async (page) => {
    await page.getByRole('radio', { name: /Need a hand/ }).click();
  }],
  ['accounts-workspace', `/${O}/accounts?workspace=harbor`],
  ['accounts-empty', '/juniper-labs/accounts'],
  ['home-desk', `/${O}`, async (page) => {
    await page.getByRole('button', { name: 'Switch workspace' }).first().click();
    await page.getByRole('menuitem', { name: 'Aster Coffee' }).click();
  }],
  ['home-empty', '/juniper-labs'],
  ['home-loading', `/${O}`, null, 'loading'],
  ['home-error', `/${O}`, null, 'error'],
  ['needs-you', `/${O}/needs-you`],
  ['needs-you-selected', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^Instagram post, Aster Coffee/ }).click();
    await page.keyboard.press('x');
    await page.keyboard.press('j');
    await page.keyboard.press('x');
  }],
  ['needs-you-instagram', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^Instagram post, Aster Coffee/ }).click();
  }],
  ['needs-you-posting', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^Instagram post, Aster Coffee/ }).click();
    await page.getByRole('button', { name: 'Post it' }).filter({ visible: true }).click();
    await page.getByText(/in \d s$/).waitFor();
  }],
  ['needs-you-posted', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^Instagram post, Aster Coffee/ }).click();
    await page.getByRole('button', { name: 'Post it' }).filter({ visible: true }).click();
    await page.getByRole('link', { name: 'View post' }).first().waitFor({ timeout: 15_000 });
  }],
  ['needs-you-blocked', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^Saved login for Facebook/ }).click();
    await page.getByRole('note').waitFor();
  }],
  ['needs-you-login-code', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^Login code for Instagram/ }).click();
    await page.getByRole('heading', { name: 'Type the code Instagram sent you' }).waitFor();
  }],
  ['needs-you-empty', '/juniper-labs/needs-you'],
  ['approve', `/${O}/approve`],
  ['approve-empty', '/juniper-labs/approve'],
  ['lane', `/${O}/accounts/aster/instagram`],
  ['lane-captcha', `/${O}/accounts/harbor/reddit`],
  ['lane-cold', `/${O}/accounts/aster/medium`],
  ['lane-terminal', `/${O}/accounts/personal/shell`, async (page) => {
    await page.getByRole('button', { name: 'Wake it up' }).click();
    await page.getByRole('button', { name: 'Put to rest' }).waitFor({ timeout: 6000 });
  }],
  ['lane-control-human', `/${O}/accounts/aster/instagram`, async (page) => {
    await page.getByRole('button', { name: 'Take over' }).click();
    await page.getByText('You have control').waitFor();
  }],
  ['lane-control-takeback', `/${O}/accounts/meridian/linkedin`],
  ['lane-control-paused', `/${O}/accounts/solstice/blog`],
  ['lane-control-stop', `/${O}/accounts/aster/instagram`, async (page) => {
    await page.getByRole('button', { name: 'Stop agent' }).click();
  }],
  ['lane-freeze-dialog', `/${O}/accounts/aster/instagram`, async (page) => {
    await page.getByRole('button', { name: 'Freeze' }).click();
    await page.getByRole('dialog').getByText('Every agent on this account stops at once.').waitFor();
  }],
  ['lane-frozen', `/${O}/accounts/aster/instagram`, async (page) => {
    await page.getByRole('button', { name: 'Freeze' }).click();
    await page.getByRole('button', { name: 'Freeze now' }).click();
    await page.getByText('Agents are stopped on this account.').waitFor();
  }],
  ['lane-wrong-account', `/${O}/accounts/aster/reddit`],
  ['lane-hold', `/${O}/accounts/meridian/reddit`],
  ['lane-inspect', `/${O}/accounts/aster/instagram`, async (page) => {
    await page.getByRole('button', { name: 'Technical details' }).click();
  }],
  ['activity', `/${O}/history`],
  ['activity-run', `/${O}/history`, async (page) => {
    await page.getByRole('button', { name: /^What happened: / }).first().click();
  }],
  ['activity-hover', `/${O}/history`, async (page) => {
    await page.getByRole('group', { name: /Runs by outcome/ }).hover({ position: { x: 500, y: 70 } });
  }],
  ['activity-range', `/${O}/history`, async (page) => {
    const chart = page.getByRole('group', { name: /Runs by outcome/ });
    const box = await chart.boundingBox();
    await page.mouse.move(box.x + box.width * 0.4, box.y + 60);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.62, box.y + 60, { steps: 6 });
    await page.mouse.up();
    await page.mouse.move(10, 10);
    await page.waitForTimeout(400);
  }],
  ['activity-empty', '/juniper-labs/history'],
  ['flows', `/${O}/routines`],
  ['flows-schedules', `/${O}/routines`, async (page) => {
    await page.getByRole('tab', { name: /Schedules/ }).click();
  }],
  ['nodes', `/${O}/machines`],
  ['nodes-add', `/${O}/machines?add=true`],
  ['nodes-empty', '/juniper-labs/machines'],
  ['machines-open', `/${O}/machines/hetzner-1`],
  ['machine-page', `/${O}/machines/hetzner-1?tab=lanes`],
  ['node-frozen', `/${O}/machines/home-mini?tab=lanes`, async (page) => {
    await page.getByRole('button', { name: 'Freeze' }).click();
    await page.getByRole('button', { name: 'Freeze now' }).click();
    await page.getByText('Agents are stopped on this machine.').waitFor();
  }],
  ['node-detail', `/${O}/machines/home-mini?tab=lanes`],
  ['node-health', `/${O}/machines/home-mini?tab=health`],
  ['node-backups', `/${O}/machines/home-mini?tab=backups`],
  ['node-environments', `/${O}/machines/home-mini?tab=environments`],
  ['node-identity', `/${O}/machines/home-mini?tab=connection`],
  ['node-settings', `/${O}/machines/home-mini?tab=settings`],
  ['settings-index', `/${O}/settings`],
  ['settings-general', `/${O}/settings/general`],
  ['settings-members', `/${O}/settings/members`],
  ['settings-invites', `/${O}/settings/invites`],
  ['settings-workspaces', `/${O}/settings/workspaces`],
  ['settings-grants', `/${O}/settings/grants`],
  ['settings-memory', `/${O}/settings/memory`],
  ['settings-notifications', `/${O}/settings/notifications`],
  ['settings-billing', `/${O}/settings/billing`],
  ['settings-audit', `/${O}/settings/audit`],
  ['settings-danger', `/${O}/settings/danger`],
  ['settings-brains', `/${O}/settings/brains`],
  ['settings-vault', `/${O}/settings/vault`],
  ['logins', `/${O}/logins`],
  ['node-browsers', `/${O}/machines/hetzner-1?tab=browsers`],
  ['settings-account', `/${O}/settings/account`],
  ['settings-passkeys', `/${O}/settings/passkeys`],
  ['settings-devices', `/${O}/settings/devices`],
  ['float-palette', `/${O}`, async (page) => {
    await page.keyboard.press('ControlOrMeta+k');
  }],
  ['float-palette-search', `/${O}`, async (page) => {
    await page.keyboard.press('ControlOrMeta+k');
    await page.keyboard.type('insta');
  }],
  ['float-palette-none', `/${O}`, async (page) => {
    await page.keyboard.press('ControlOrMeta+k');
    await page.keyboard.type('zzzz');
  }],
  ['float-shortcuts', `/${O}`, async (page) => {
    await page.keyboard.press('?');
  }],
  ['float-bell', `/${O}`, async (page) => {
    await page.getByRole('button', { name: /^Needs you/ }).click();
  }],
  ['float-status', `/${O}`, async (page) => {
    await page.getByRole('button', { name: /^System: / }).filter({ visible: true }).first().click();
  }],
  ['float-more', `/${O}`, async (page) => {
    // The phone's tab bar only; on a wide screen the rail shows everything.
    const more = page.getByRole('button', { name: /^More/ }).filter({ visible: true });
    if (await more.count()) await more.first().click();
  }],
  ['float-account', `/${O}`, async (page) => {
    await page.getByRole('button', { name: 'Account' }).click();
  }],
  ['float-org', `/${O}`, async (page) => {
    await page.getByRole('button', { name: 'Switch org' }).click();
  }],
  ['dialog-new-lane', `/${O}/accounts`, async (page) => {
    await page.getByRole('button', { name: 'Add an account' }).click();
  }],
  ['dialog-run-flow', `/${O}/routines`, async (page) => {
    await page.getByRole('button', { name: 'Run meridian.facebook.boost' }).click();
  }],
  ['dialog-new-schedule', `/${O}/routines`, async (page) => {
    await page.getByRole('button', { name: 'New schedule' }).click();
  }],
  ['dialog-new-grant', `/${O}/settings/grants`, async (page) => {
    await page.getByRole('button', { name: 'New grant' }).click();
  }],
  ['dialog-grant-token', `/${O}/settings/grants`, async (page) => {
    await page.getByRole('button', { name: 'New grant' }).click();
    await page.getByLabel('Subject').fill('claude-code-harbor');
    await page.getByRole('button', { name: 'Create grant' }).click();
    await page.getByText('Copy your token').waitFor();
  }],
  // S3: push, notifications, webhooks, standing approvals, retention, Review links, install.
  ['s3-notifications', `/${O}/settings/notifications`],
  ['s3-notifications-push-on', `/${O}/settings/notifications`, async (page) => {
    await page.context().grantPermissions(['notifications']);
    // Headless Chrome has no push service, so the subscription is the one stand-in.
    await page.evaluate(() => {
      const fake = { options: { applicationServerKey: null }, toJSON: () => ({ endpoint: 'https://push.example/shot', keys: { p256dh: 'k', auth: 'a' } }), unsubscribe: async () => true };
      PushManager.prototype.subscribe = async () => fake;
    });
    await page.getByRole('switch', { name: 'Push on this device' }).click();
    await page.getByText('Push is on for this device').waitFor();
  }],
  ['s3-notifications-no-server', `/${O}/settings/notifications`, null, 'push-off'],
  ['s3-notifications-plan', '/juniper-labs/settings/notifications'],
  ['s3-notifications-quiet-off', `/${O}/settings/notifications`, async (page) => {
    await page.getByRole('switch', { name: 'Quiet hours' }).click();
    await page.getByText('Saved', { exact: true }).waitFor();
  }],
  ['s3-notifications-loading', `/${O}/settings/notifications`, null, 'loading'],
  ['s3-webhooks', `/${O}/settings/webhooks`],
  ['s3-webhooks-add', `/${O}/settings/webhooks`, async (page) => {
    await page.getByRole('button', { name: 'Add webhook' }).first().click();
    await page.getByLabel('Address').fill('http://receiver.example.net/agentos');
    await page.getByRole('dialog').getByRole('button', { name: 'Add webhook' }).click();
    await page.getByText('Confirm it is you').waitFor();
    await page.getByRole('button', { name: 'Use your passkey' }).click();
    await page.getByRole('dialog', { name: 'Add a webhook' }).getByRole('alert').waitFor();
  }],
  ['s3-webhooks-secret', `/${O}/settings/webhooks`, async (page) => {
    await page.getByRole('button', { name: 'Add webhook' }).first().click();
    await page.getByLabel('Address').fill('https://receiver.example.net/agentos');
    await page.getByRole('dialog').getByRole('button', { name: 'Add webhook' }).click();
    await page.getByText('Confirm it is you').waitFor();
    await page.getByRole('button', { name: 'Use your passkey' }).click();
    await page.getByText('Copy the signing secret').waitFor();
  }],
  ['s3-webhooks-deliveries', `/${O}/settings/webhooks`, async (page) => {
    await page.getByRole('button', { name: /^Actions for/ }).first().click();
    await page.getByRole('menuitem', { name: 'Recent deliveries' }).click();
    await page.getByRole('dialog', { name: 'Recent deliveries' }).getByText('The receiver answered 502.').waitFor();
  }],
  ['s3-webhooks-rotate', `/${O}/settings/webhooks`, async (page) => {
    await page.getByRole('button', { name: /^Actions for/ }).first().click();
    await page.getByRole('menuitem', { name: 'Rotate secret' }).click();
  }],
  ['s3-webhooks-empty', '/juniper-labs/settings/webhooks'],
  ['s3-standing', `/${O}/settings/standing`],
  ['s3-standing-all', `/${O}/settings/standing?show=all`],
  ['s3-standing-new', `/${O}/settings/standing`, async (page) => {
    await page.getByRole('button', { name: /^New/ }).first().click();
    await page.getByRole('dialog').getByText(/No node applies standing approvals yet/).waitFor();
  }],
  ['s3-standing-empty', '/juniper-labs/settings/standing'],
  ['s3-allow-again', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^LinkedIn post, Aster Coffee/ }).click();
    await page.getByRole('button', { name: 'Details for the technical' }).click();
    await page.getByRole('button', { name: 'Always allow posts like this' }).click();
    await page.getByRole('dialog').getByText(/No node applies standing approvals yet/).waitFor();
  }],
  ['s3-confirm-heavy', `/${O}/needs-you`, async (page) => {
    await page.keyboard.press('a');
    await page.getByRole('dialog', { name: 'Are you sure?' }).waitFor();
  }],
  ['s3-batch', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: 'Choose several' }).click();
    await page.getByRole('checkbox', { name: /^Choose: Instagram post, Aster Coffee/ }).click();
    await page.getByRole('checkbox', { name: /^Choose: LinkedIn post, Aster Coffee/ }).click();
    await page.getByText('2 posts chosen').waitFor();
  }],
  ['s3-cut-preview', `/${O}/needs-you`, async (page) => {
    await page.getByRole('button', { name: /^Medium story, Solstice Studio/ }).click();
    await page.getByText(/The preview is cut here/).filter({ visible: true }).first().waitFor();
  }],
  ['s3-install', `/${O}/needs-you`, async (page) => {
    await page.evaluate(() => sessionStorage.setItem('agentos.install.earned', '1'));
    await page.reload({ waitUntil: 'load' });
    await page.waitForTimeout(400);
    await page.evaluate(() => window.dispatchEvent(Object.assign(new Event('beforeinstallprompt', { cancelable: true }), { prompt: async () => {}, userChoice: Promise.resolve({ outcome: 'dismissed' }) })));
    await page.getByRole('complementary', { name: 'Install agentos' }).waitFor();
  }],
  ['s3-review', `/${O}/needs-you`, async (page) => {
    const id = await page.evaluate(async () => (await (await fetch('/api/orgs/northwind-group/approvals')).json()).find((item) => item.effect === 'commit').id);
    await page.goto(`${base}/${O}/review/${id}?e=${Math.floor(Date.now() / 1000) + 1800}&s=mock`, { waitUntil: 'load' });
    await page.getByRole('button', { name: 'Post it' }).waitFor();
  }],
  ['s3-review-approved', `/${O}/needs-you`, async (page) => {
    const id = await page.evaluate(async () => (await (await fetch('/api/orgs/northwind-group/approvals')).json()).find((item) => item.effect === 'commit').id);
    await page.goto(`${base}/${O}/review/${id}?e=${Math.floor(Date.now() / 1000) + 1800}&s=mock`, { waitUntil: 'load' });
    await page.getByRole('button', { name: 'Post it' }).click();
    await page.getByText(/in \d s$/).waitFor();
  }],
  ['s3-review-refused', `/${O}/needs-you`, async (page) => {
    const id = await page.evaluate(async () => (await (await fetch('/api/orgs/northwind-group/approvals?state=all')).json()).find((item) => item.delivery === 'refused').id);
    await page.goto(`${base}/${O}/review/${id}?e=${Math.floor(Date.now() / 1000) + 1800}&s=mock`, { waitUntil: 'load' });
    await page.getByText(/The computer did not take your/).waitFor();
  }],
  ['s3-review-expired', `/${O}/review/ap_EXPIRED?e=1&s=mock`],
  ['s3-retention', `/${O}/settings/retention`],
  ['s3-retention-hold', '/juniper-labs/settings/retention'],
  ['s3-lane-standing', `/${O}/accounts/aster/linkedin`],
  ['s3-offline', '/offline.html'],
  ['dialog-node-waiting', `/${O}/machines`, async (page) => {
    await page.getByRole('button', { name: 'Add a machine' }).click();
    await page.getByRole('button', { name: 'Get install command' }).click();
    await page.getByText('Waiting for the machine').waitFor();
  }],
  ['dialog-confirm-remove', `/${O}/machines/hetzner-1?tab=lanes`, async (page) => {
    await page.getByRole('button', { name: 'Remove machine' }).click();
  }],
  ['activity-filtered', `/${O}/history?effect=exec`],
  ['activity-loading', `/${O}/history`, null, 'loading'],
  ['activity-error', `/${O}/history`, null, 'error'],
  ['needs-you-loading', `/${O}/needs-you`, null, 'loading'],
  ['needs-you-error', `/${O}/needs-you`, null, 'error'],
  ['nodes-loading', `/${O}/machines`, null, 'loading'],
  ['flows-loading', `/${O}/routines`, null, 'loading'],
  ['lane-loading', `/${O}/accounts/aster/instagram`, null, 'loading'],
  ['approve-loading', `/${O}/approve`, null, 'loading'],
  ['settings-members-loading', `/${O}/settings/members`, null, 'loading'],
  ['step-up', `/${O}/settings/danger`, async (page) => {
    await page.getByRole('button', { name: 'Delete org' }).click();
    await page.getByLabel(/to confirm/).fill(O);
    await page.getByRole('dialog').getByRole('button', { name: 'Delete org' }).click();
    await page.getByText('Confirm it is you').waitFor();
  }],
  // Last: these add init scripts that stay on the page for every route after them.
  ['s3-notifications-blocked', `/${O}/settings/notifications`, async (page) => {
    await page.addInitScript(() => Object.defineProperty(Notification, 'permission', { get: () => 'denied' }));
    await page.reload({ waitUntil: 'load' });
    await page.getByText('Notifications are blocked for this site').waitFor();
  }],
  ['s3-notifications-iphone', `/${O}/settings/notifications`, async (page) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'userAgent', { get: () => 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1' });
      delete window.PushManager;
    });
    await page.reload({ waitUntil: 'load' });
    await page.getByText('Add agentos to your Home Screen first').waitFor();
  }],
];

const selected = only ? routes.filter(([name]) => only.some((term) => name.includes(term))) : routes;

mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const failures = [];

async function shoot(width, theme) {
  const height = width > 500 ? 900 : 812;
  const context = await browser.newContext({
    viewport: { width, height },
    colorScheme: theme,
    deviceScaleFactor: width > 500 ? 1 : 2,
    reducedMotion: 'reduce',
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => failures.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) {
      failures.push(`console: ${message.text().slice(0, 240)}`);
    }
  });
  for (const [name, path, action, scenario] of selected) {
    const signedOut = scenario === 'signed-out';
    const mock = ['still', scenario && !signedOut ? scenario : null].filter(Boolean).join(',');
    await page.goto(`${base}/sign-in?mock=${mock}`, { waitUntil: 'domcontentloaded' });
    // Set directly: the app may not have run yet at domcontentloaded.
    await page.evaluate(([flag, value]) => {
      sessionStorage.setItem('agentos.mock.scenario', value);
      if (flag) localStorage.setItem('agentos.mock.signed-out', '1');
      else localStorage.removeItem('agentos.mock.signed-out');
      localStorage.removeItem('agentos.workspace.northwind-group');
      for (const key of Object.keys(localStorage)) if (key.startsWith('agentos.technical.')) localStorage.removeItem(key);
    }, [signedOut, mock]);
    // Waits on load, not networkidle: the event stream never goes idle.
    await page.goto(`${base}${path}`, { waitUntil: 'load' }).catch(() => {});
    await page.waitForTimeout(600);
    // The client retries a 500 before it gives up, as it should in production.
    if (scenario === 'error') {
      await page.getByText('Could not load this').first().waitFor({ timeout: 15_000 }).catch(() => {});
    }
    try {
      if (action) await action(page);
      await page.waitForTimeout(250);
      await page.screenshot({ path: join(out, `${name}.${width}.${theme}.png`), fullPage: true });
    } catch (error) {
      failures.push(`${name} at ${width} ${theme}: ${String(error.message).split('\n')[0]}`);
    }
  }
  await context.close();
}

for (const width of widths) for (const theme of themes) await shoot(width, theme);

if (sheets) {
  // Contact sheets, so a critique pass can read many screens per image.
  const page = await browser.newPage({ viewport: { width: 1560, height: 1000 } });
  const files = readdirSync(out).filter((file) => file.endsWith('.png') && !file.startsWith('sheet'));
  for (const width of widths) {
    for (const theme of themes) {
      const group = files.filter((file) => file.includes(`.${width}.${theme}.`)).sort();
      const perSheet = width > 500 ? 6 : 8;
      const columns = width > 500 ? 2 : 4;
      for (let index = 0; index * perSheet < group.length; index += 1) {
        const cells = group.slice(index * perSheet, (index + 1) * perSheet).map((file) => {
          const data = readFileSync(join(out, file)).toString('base64');
          return `<figure><figcaption>${file.split('.')[0]}</figcaption><img src="data:image/png;base64,${data}"></figure>`;
        });
        await page.setContent(`<style>
          body{margin:0;padding:12px;background:#888;font:12px system-ui;color:#fff;display:grid;gap:12px;grid-template-columns:repeat(${columns},1fr);align-items:start}
          figure{margin:0;min-width:0} figcaption{padding:2px 0} img{width:100%;display:block;max-height:${width > 500 ? 520 : 900}px;object-fit:cover;object-position:top}
        </style>${cells.join('')}`);
        await page.waitForTimeout(100);
        await page.screenshot({ path: join(out, `sheet-${width}-${theme}-${index + 1}.png`), fullPage: true });
      }
    }
  }
  await page.close();
}

await browser.close();
const unique = [...new Set(failures)];
writeFileSync(join(out, 'failures.txt'), unique.join('\n'));
console.log(`${selected.length} routes, ${widths.length * themes.length} variants, ${unique.length} problems`);
for (const failure of unique.slice(0, 30)) console.log(`  ${failure}`);
