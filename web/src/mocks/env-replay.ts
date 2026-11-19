/**
 * The replays the mock keeps (concept stage): what happened in an environment's browser, as the node's
 * timeline gives it (agentos.environments.replay): frames with a cause, and the calls between them.
 *
 * r9cl "Guest checkout, end to end" is the long one: 47 minutes on a fictional shop, Harbor Supply.
 * The agent finds that a guest cannot check out, makes a guest form, gets stuck on a postcode, a person
 * takes over for half a minute, the agent fixes the check and pays with a test card. The frames are
 * drawn here as SVG wireframes from each frame's page and state: never a real site, never a real login.
 */
import type { EnvReplayTimeline, ReplayCall, ReplayCause, ReplayFrame } from '@/api/types';

type Page =
  | 'home' | 'product' | 'cart' | 'signin' | 'checkout' | 'shipping' | 'payment' | 'confirm' | 'overlay' | 'offer' | 'dashboard';

interface Look {
  page: Page;
  cart?: number;
  size?: string;
  toast?: string;
  form?: Partial<Record<'email' | 'name' | 'address' | 'postcode' | 'city' | 'country', string>>;
  error?: string;
  countryOpen?: boolean;
  shipping?: boolean;
  card?: boolean;
  paying?: boolean;
  dark?: boolean;
  step?: number;
}

/** One line of a script: a frame (with its look) or a call alone. `t` is seconds after the open. */
type Line =
  | { t: number; frame: ReplayCause; tool?: string; summary?: string; url: string; title: string; look: Look }
  | { t: number; call: string; summary: string; ok?: boolean };

const SHOP = 'http://localhost:3000';
const TITLE = 'Harbor Supply';
const FORM_FULL = { email: 'ada@example.com', name: 'Ada Lovelace', address: '12 Harbour Street', city: 'London' };

function guestCheckout(): Line[] {
  const co = `${SHOP}/checkout`;
  const coTitle = `Checkout · ${TITLE}`;
  return [
    { t: 8, frame: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000', url: `${SHOP}/`, title: TITLE, look: { page: 'home' } },
    { t: 21, call: 'browser_snapshot', summary: 'localhost:3000, 64 refs' },
    { t: 58, call: 'terminal_exec', summary: 'rg -n "guest" src/checkout' },
    { t: 95, frame: 'agent', tool: 'browser_act', summary: 'click "Canvas field jacket"', url: `${SHOP}/p/canvas-field-jacket`, title: `Canvas field jacket · ${TITLE}`, look: { page: 'product' } },
    { t: 131, frame: 'agent', tool: 'browser_act', summary: 'click "M"', url: `${SHOP}/p/canvas-field-jacket`, title: `Canvas field jacket · ${TITLE}`, look: { page: 'product', size: 'M' } },
    { t: 150, frame: 'agent', tool: 'browser_act', summary: 'click "Add to cart"', url: `${SHOP}/p/canvas-field-jacket`, title: `Canvas field jacket · ${TITLE}`, look: { page: 'product', size: 'M', cart: 1, toast: 'Added to cart' } },
    { t: 153, frame: 'change', url: `${SHOP}/p/canvas-field-jacket`, title: `Canvas field jacket · ${TITLE}`, look: { page: 'product', size: 'M', cart: 1 } },
    { t: 171, frame: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000/cart', url: `${SHOP}/cart`, title: `Your cart · ${TITLE}`, look: { page: 'cart', cart: 1 } },
    { t: 202, frame: 'agent', tool: 'browser_act', summary: 'click "Checkout"', url: `${SHOP}/sign-in?next=/checkout`, title: `Sign in · ${TITLE}`, look: { page: 'signin', cart: 1 } },
    { t: 214, call: 'browser_snapshot', summary: 'localhost:3000/sign-in, no way on as a guest' },
    { t: 268, call: 'terminal_exec', summary: 'sed -n 1,80p src/checkout/route.ts' },
    { t: 390, call: 'terminal_exec', summary: 'git switch -c feat/guest-checkout' },
    { t: 520, call: 'terminal_exec', summary: 'apply_patch src/checkout/guest-form.tsx (+118)' },
    { t: 641, frame: 'change', url: `${SHOP}/sign-in?next=/checkout`, title: `Sign in · ${TITLE}`, look: { page: 'overlay', cart: 1 } },
    { t: 690, call: 'terminal_exec', summary: 'apply_patch src/checkout/index.ts (export GuestForm)' },
    { t: 694, frame: 'change', url: `${SHOP}/sign-in?next=/checkout`, title: `Sign in · ${TITLE}`, look: { page: 'signin', cart: 1 } },
    { t: 760, frame: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000/checkout', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: {} } },
    { t: 801, frame: 'agent', tool: 'browser_act', summary: 'type "ada@example.com" into Email', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { email: FORM_FULL.email } } },
    { t: 829, frame: 'agent', tool: 'browser_act', summary: 'type "Ada Lovelace" into Full name', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { email: FORM_FULL.email, name: FORM_FULL.name } } },
    { t: 858, frame: 'agent', tool: 'browser_act', summary: 'type "12 Harbour Street" into Address', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, city: '' } } },
    { t: 884, frame: 'agent', tool: 'browser_act', summary: 'type "London" into City', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: FORM_FULL } },
    { t: 902, frame: 'agent', tool: 'browser_act', summary: 'type "SW1A1AA" into Postcode', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A1AA' } } },
    { t: 921, frame: 'agent', tool: 'browser_act', summary: 'click "Continue to shipping"', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A1AA' }, error: 'Enter a valid postcode' } },
    { t: 948, frame: 'agent', tool: 'browser_act', summary: 'click "Continue to shipping"', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A1AA' }, error: 'Enter a valid postcode' } },
    { t: 962, call: 'terminal_exec', summary: 'rg -n "valid postcode" src', ok: true },
    { t: 1001, frame: 'person', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A1AA' }, error: 'Enter a valid postcode', countryOpen: true } },
    { t: 1007, frame: 'person', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A1AA', country: 'United Kingdom' }, error: 'Enter a valid postcode' } },
    { t: 1016, frame: 'person', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A 1AA', country: 'United Kingdom' } } },
    { t: 1025, frame: 'person', url: `${SHOP}/checkout/shipping`, title: `Shipping · ${TITLE}`, look: { page: 'shipping', cart: 1 } },
    { t: 1058, call: 'browser_snapshot', summary: 'localhost:3000/checkout/shipping: the person chose the country, then the postcode passed' },
    { t: 1120, call: 'terminal_exec', summary: 'apply_patch src/checkout/postcode.ts (a postcode without its space, and the country first)' },
    { t: 1236, call: 'terminal_job_start', summary: 'pnpm vitest run src/checkout' },
    { t: 1290, call: 'terminal_job_start', summary: 'pnpm vitest run src/checkout: 14 passed' },
    { t: 1346, frame: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000/checkout', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A1AZZ', country: 'United Kingdom' }, error: 'Enter a UK postcode, like SW1A 1AA' } },
    { t: 1381, call: 'environment_proof', summary: 'The postcode error, in words a person reads' },
    { t: 1489, frame: 'agent', tool: 'browser_act', summary: 'type "SW1A1AA" into Postcode', url: co, title: coTitle, look: { page: 'checkout', cart: 1, form: { ...FORM_FULL, postcode: 'SW1A1AA', country: 'United Kingdom' } } },
    { t: 1502, frame: 'agent', tool: 'browser_act', summary: 'click "Continue to shipping"', url: `${SHOP}/checkout/shipping`, title: `Shipping · ${TITLE}`, look: { page: 'shipping', cart: 1 } },
    { t: 1561, frame: 'agent', tool: 'browser_act', summary: 'click "Standard, 2 to 3 days"', url: `${SHOP}/checkout/shipping`, title: `Shipping · ${TITLE}`, look: { page: 'shipping', cart: 1, shipping: true } },
    { t: 1603, frame: 'agent', tool: 'browser_act', summary: 'click "Continue to payment"', url: `${SHOP}/checkout/payment`, title: `Payment · ${TITLE}`, look: { page: 'payment', cart: 1 } },
    { t: 1712, call: 'terminal_exec', summary: 'grep STRIPE .env.local (a placeholder, the node holds the value)' },
    { t: 1902, frame: 'agent', tool: 'browser_act', summary: 'type "4242 4242 4242 4242" into Card number', url: `${SHOP}/checkout/payment`, title: `Payment · ${TITLE}`, look: { page: 'payment', cart: 1, card: true } },
    { t: 1958, frame: 'agent', tool: 'browser_act', summary: 'click "Pay €129.00"', url: `${SHOP}/checkout/payment`, title: `Payment · ${TITLE}`, look: { page: 'payment', cart: 1, card: true, paying: true } },
    { t: 1964, frame: 'change', url: `${SHOP}/order/HS-10482`, title: `Order HS-10482 · ${TITLE}`, look: { page: 'confirm', cart: 0 } },
    { t: 1980, call: 'environment_proof', summary: 'The order confirmation for a guest' },
    { t: 2110, call: 'terminal_job_start', summary: 'pnpm playwright test e2e/guest-checkout.spec.ts' },
    { t: 2370, call: 'terminal_job_start', summary: 'pnpm playwright test e2e/guest-checkout.spec.ts: 3 passed' },
    { t: 2480, call: 'terminal_exec', summary: 'git commit -m "Checkout: guests check out without an account"' },
    { t: 2560, call: 'browser_snapshot', summary: 'localhost:3000/order/HS-10482, 31 refs' },
    { t: 2640, call: 'terminal_exec', summary: 'git push origin feat/guest-checkout' },
    { t: 2780, call: 'environment_close', summary: 'closed, the work is pushed' },
    { t: 2806, frame: 'close', url: `${SHOP}/order/HS-10482`, title: `Order HS-10482 · ${TITLE}`, look: { page: 'confirm', cart: 0 } },
  ];
}

function offerPage(): Line[] {
  const url = `${SHOP}/offer`;
  const title = `Black Friday · ${TITLE}`;
  return [
    { t: 52, frame: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000/', url: `${SHOP}/`, title: TITLE, look: { page: 'home' } },
    { t: 120, frame: 'change', url: `${SHOP}/`, title: TITLE, look: { page: 'offer', step: 0 } },
    { t: 530, frame: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000/offer', url, title, look: { page: 'offer', step: 1 } },
    { t: 545, call: 'ui_check', summary: 'http://localhost:3000/offer at 360: 1 major, the price wraps under the badge', ok: false },
    { t: 610, frame: 'change', url, title, look: { page: 'offer', step: 2 } },
    { t: 640, frame: 'agent', tool: 'browser_act', summary: 'click "Claim the deal"', url, title, look: { page: 'offer', step: 3 } },
    { t: 700, frame: 'change', url, title, look: { page: 'offer', step: 4 } },
    { t: 760, call: 'terminal_exec', summary: 'git push origin feat/black-friday-offer' },
    { t: 790, frame: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000/offer', url, title, look: { page: 'offer', step: 4 } },
  ];
}

function darkDashboard(): Line[] {
  const url = `${SHOP.replace('3000', '5173')}/`;
  const lines: Line[] = [];
  for (let index = 0; index < 64; index += 1) {
    const t = 40 + index * 210;
    const dark = index > 6;
    lines.push({ t, frame: index % 4 === 0 ? 'agent' : 'change', tool: index % 4 === 0 ? 'browser_navigate' : undefined, summary: index % 4 === 0 ? 'http://localhost:5173/' : undefined, url, title: 'Admin · Harbor Supply', look: { page: 'dashboard', dark, step: index } });
  }
  lines.push({ t: 40 + 64 * 210, frame: 'save', url, title: 'Admin · Harbor Supply', look: { page: 'dashboard', dark: true, step: 64 } });
  return lines;
}

const SCRIPTS: Record<string, () => Line[]> = { r9cl: guestCheckout, e7k2: offerPage, d1dm: darkDashboard };

/** The timeline of one environment, oldest first, as GET .../replay answers. */
export function replayTimeline(org: string, env: { id: string; title: string; state: string; created_at: string; closed_at?: string | null }): EnvReplayTimeline | null {
  const script = SCRIPTS[env.id]?.();
  if (!script) return null;
  const start = Date.parse(env.created_at);
  const src = `/api/orgs/${org}/environments/${env.id}/replay`;
  const timeline: (ReplayFrame | ReplayCall)[] = script.map((line, index) => {
    const at = new Date(start + line.t * 1000).toISOString();
    if ('call' in line) return { kind: 'call', at, tool: line.call, summary: line.summary, ok: line.ok ?? true };
    const id = `fr_${env.id}_${String(index).padStart(3, '0')}`;
    return { kind: 'frame', id, at, cause: line.frame, tool: line.tool ?? null, summary: line.summary ?? null, page_url: line.url, page_title: line.title, width: 1280, height: 800, bytes: 14_000 + ((index * 977) % 6000), src: `${src}/${id}.jpg` };
  });
  const frames = timeline.filter((item): item is ReplayFrame => item.kind === 'frame');
  return {
    environment: { id: env.id, title: env.title, state: env.state, created_at: env.created_at, closed_at: env.closed_at ?? null },
    replay: {
      enabled: true,
      frames: frames.length,
      since: frames[0]?.at ?? null,
      until: frames[frames.length - 1]?.at ?? null,
      bytes: frames.reduce((sum, frame) => sum + (frame.bytes ?? 0), 0),
      kept_until: env.closed_at ? new Date(Date.parse(env.closed_at) + 14 * 86_400_000).toISOString() : null,
    },
    timeline,
    next: null,
  };
}

// ---------------------------------------------------------------- the pictures

const esc = (text: string) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const text = (x: number, y: number, value: string, size = 16, fill = '#0f172a', weight = 400, extra = '') =>
  `<text x="${x}" y="${y}" font-family="system-ui, sans-serif" font-size="${size}" font-weight="${weight}" fill="${fill}" ${extra}>${esc(value)}</text>`;
const box = (x: number, y: number, w: number, h: number, fill: string, r = 8, stroke = '') =>
  `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" fill="${fill}"${stroke ? ` stroke="${stroke}" stroke-width="1.5"` : ''}/>`;

function header(look: Look): string {
  const badge = look.cart ? `${box(1196, 18, 22, 22, '#0f172a', 11)}${text(1203, 34, String(look.cart), 13, '#fff', 600)}` : '';
  return `${box(0, 0, 1280, 64, '#ffffff', 0)}<rect y="64" width="1280" height="1" fill="#e2e8f0"/>${text(48, 40, 'Harbor Supply', 20, '#0f172a', 700)}
${['Men', 'Women', 'Gear', 'Sale'].map((item, index) => text(300 + index * 84, 39, item, 15, '#475569')).join('')}
<path d="M1170 26h20l-2 14h-16z" fill="none" stroke="#0f172a" stroke-width="2"/>${badge}`;
}

function field(x: number, y: number, w: number, label: string, value: string | undefined, error = false): string {
  return `${text(x, y, label, 13, '#475569', 500)}${box(x, y + 8, w, 44, '#ffffff', 6, error ? '#dc2626' : '#cbd5e1')}${value ? text(x + 14, y + 36, value, 15) : ''}`;
}

function summary(x: number, y: number, total = '€133.90'): string {
  return `${box(x, y, 400, 260, '#f8fafc', 12, '#e2e8f0')}${text(x + 24, y + 40, 'Order summary', 17, '#0f172a', 600)}
${box(x + 24, y + 64, 56, 56, '#cbd5e1', 6)}${text(x + 96, y + 88, 'Canvas field jacket', 15, '#0f172a', 500)}${text(x + 96, y + 110, 'Size M · 1', 13, '#64748b')}
${text(x + 24, y + 170, 'Shipping', 14, '#475569')}${text(x + 300, y + 170, '€4.90', 14, '#475569')}
${text(x + 24, y + 214, 'Total', 17, '#0f172a', 600)}${text(x + 290, y + 214, total, 17, '#0f172a', 600)}`;
}

const PAGES: Record<Page, (look: Look) => string> = {
  home: () => `${box(48, 96, 1184, 300, '#1e293b', 16)}${text(96, 210, 'Winter gear, ready to ship', 44, '#fff', 700)}${text(96, 252, 'Jackets, packs and boots that last.', 18, '#cbd5e1')}${box(96, 288, 150, 48, '#fff', 8)}${text(124, 318, 'Shop now', 16, '#0f172a', 600)}
${[0, 1, 2, 3].map((index) => `${box(48 + index * 302, 428, 278, 250, '#e2e8f0', 12)}${text(48 + index * 302, 708, ['Canvas field jacket', 'Wool watch cap', 'Trail pack 28 L', 'Harbor boots'][index] ?? '', 16, '#0f172a', 500)}${text(48 + index * 302, 732, ['€129.00', '€24.00', '€89.00', '€159.00'][index] ?? '', 15, '#475569')}`).join('')}`,
  product: (look) => `${box(48, 96, 600, 600, '#e2e8f0', 16)}<path d="M268 220l80-30 80 30 50 120-40 20-20-50v260H278V310l-20 50-40-20z" fill="#94a3b8"/>
${text(700, 150, 'Canvas field jacket', 34, '#0f172a', 700)}${text(700, 196, '€129.00', 22, '#334155', 500)}${text(700, 250, 'Waxed cotton, a corduroy collar, four pockets.', 16, '#475569')}${text(700, 310, 'Size', 14, '#475569', 500)}
${['S', 'M', 'L', 'XL'].map((size, index) => `${box(700 + index * 74, 326, 60, 48, look.size === size ? '#0f172a' : '#fff', 8, look.size === size ? '' : '#cbd5e1')}${text(722 + index * 74 - (size.length - 1) * 5, 356, size, 16, look.size === size ? '#fff' : '#0f172a', 500)}`).join('')}
${box(700, 410, 380, 56, '#0f172a', 8)}${text(836, 444, 'Add to cart', 17, '#fff', 600)}
${look.toast ? `${box(940, 84, 292, 56, '#0f172a', 10)}${text(966, 118, `✓  ${look.toast}`, 15, '#fff', 500)}` : ''}`,
  cart: () => `${text(48, 140, 'Your cart', 34, '#0f172a', 700)}${box(48, 176, 700, 120, '#fff', 12, '#e2e8f0')}${box(72, 196, 80, 80, '#cbd5e1', 8)}${text(176, 228, 'Canvas field jacket', 17, '#0f172a', 500)}${text(176, 254, 'Size M · Qty 1', 14, '#64748b')}${text(640, 228, '€129.00', 17, '#0f172a', 500)}
${box(800, 176, 432, 220, '#f8fafc', 12, '#e2e8f0')}${text(828, 220, 'Subtotal', 16, '#475569')}${text(1110, 220, '€129.00', 16, '#0f172a')}${text(828, 256, 'Shipping', 16, '#475569')}${text(1020, 256, 'At checkout', 16, '#64748b')}${box(828, 300, 376, 56, '#0f172a', 8)}${text(976, 334, 'Checkout', 17, '#fff', 600)}`,
  signin: () => `${box(440, 140, 400, 420, '#fff', 16, '#e2e8f0')}${text(484, 210, 'Sign in to check out', 26, '#0f172a', 700)}${text(484, 244, 'You need an account to place an order.', 15, '#64748b')}${field(484, 290, 312, 'Email', undefined)}${box(484, 380, 312, 48, '#0f172a', 8)}${text(604, 410, 'Continue', 16, '#fff', 600)}${text(552, 480, 'Create an account', 15, '#334155', 500, 'text-decoration="underline"')}`,
  checkout: (look) => {
    const form = look.form ?? {};
    const bad = Boolean(look.error);
    return `${text(48, 132, 'Checkout', 32, '#0f172a', 700)}${text(48, 162, 'Check out as a guest, or sign in.', 15, '#64748b')}
${field(48, 200, 640, 'Email', form.email)}${field(48, 280, 640, 'Full name', form.name)}${field(48, 360, 640, 'Address', form.address)}
${field(48, 440, 300, 'Postcode', form.postcode, bad)}${field(388, 440, 300, 'City', form.city)}
${bad ? `<path d="M48 518l8-14 8 14z" fill="#dc2626"/>${text(72, 517, look.error ?? '', 14, '#dc2626', 500)}` : ''}
${field(48, 548, 640, 'Country', form.country ?? 'Choose a country', false)}<path d="M660 576l8 8 8-8" fill="none" stroke="#475569" stroke-width="2"/>
${box(48, 640, 640, 56, '#0f172a', 8)}${text(282, 674, 'Continue to shipping', 17, '#fff', 600)}
${look.countryOpen ? `${box(48, 604, 640, 176, '#fff', 8, '#94a3b8')}${['Ireland', 'Netherlands', 'United Kingdom', 'United States'].map((name, index) => `${index === 2 ? box(52, 612 + index * 40, 632, 38, '#e2e8f0', 4) : ''}${text(70, 637 + index * 40, name, 15)}`).join('')}` : ''}
${summary(780, 200)}`;
  },
  shipping: (look) => `${text(48, 132, 'Shipping', 32, '#0f172a', 700)}${['Standard, 2 to 3 days · €4.90', 'Express, next day · €12.00'].map((label, index) => `${box(48, 176 + index * 84, 640, 68, '#fff', 10, look.shipping && index === 0 ? '#0f172a' : '#cbd5e1')}<circle cx="82" cy="${210 + index * 84}" r="10" fill="none" stroke="#0f172a" stroke-width="2"/>${look.shipping && index === 0 ? `<circle cx="82" cy="${210 + index * 84}" r="5" fill="#0f172a"/>` : ''}${text(108, 216 + index * 84, label, 16)}`).join('')}
${box(48, 370, 640, 56, '#0f172a', 8)}${text(282, 404, 'Continue to payment', 17, '#fff', 600)}${summary(780, 176, '€133.90')}`,
  payment: (look) => `${text(48, 132, 'Payment', 32, '#0f172a', 700)}${text(48, 162, 'Test mode: no money moves.', 15, '#64748b')}${field(48, 200, 640, 'Card number', look.card ? '4242 4242 4242 4242' : undefined)}${field(48, 280, 300, 'Expiry', look.card ? '12 / 29' : undefined)}${field(388, 280, 300, 'CVC', look.card ? '123' : undefined)}
${box(48, 380, 640, 56, look.paying ? '#475569' : '#0f172a', 8)}${text(look.paying ? 312 : 314, 414, look.paying ? 'Processing' : 'Pay €133.90', 17, '#fff', 600)}${summary(780, 200, '€133.90')}`,
  confirm: () => `<circle cx="640" cy="200" r="44" fill="#dcfce7"/><path d="M620 200l14 14 26-28" fill="none" stroke="#15803d" stroke-width="6"/>${text(640, 300, 'Thank you, Ada', 34, '#0f172a', 700, 'text-anchor="middle"')}${text(640, 336, 'Order HS-10482 is placed. A receipt is on its way to ada@example.com.', 16, '#475569', 400, 'text-anchor="middle"')}${summary(440, 380, '€133.90')}`,
  overlay: () => `${PAGES.signin({ page: 'signin' })}<rect y="65" width="1280" height="735" fill="#0f172a" opacity="0.86"/>${box(140, 130, 1000, 380, '#111827', 12, '#ef4444')}${text(180, 190, 'Failed to compile', 26, '#f87171', 700)}${text(180, 236, './src/checkout/route.ts', 16, '#e5e7eb', 500, 'font-family="ui-monospace, monospace"')}${text(180, 276, "Export 'GuestForm' (imported as 'GuestForm') was not found in './guest-form'", 15, '#fca5a5', 400, 'font-family="ui-monospace, monospace"')}${text(180, 316, "  12 | import { GuestForm } from './guest-form';", 15, '#9ca3af', 400, 'font-family="ui-monospace, monospace"')}`,
  offer: (look) => {
    const step = look.step ?? 0;
    const wrap = step === 1 || step === 2;
    return `${box(48, 96, 1184, 200, '#7c2d12', 16)}${text(96, 190, 'Black Friday: 30% off everything', 40, '#fff', 700)}${box(96, 222, 170, 48, '#fff', 8)}${text(116, 252, 'Claim the deal', 16, '#7c2d12', 600)}
${[0, 1, 2].map((index) => `${box(48 + index * 400, 330, 376, 380, '#fff', 14, index === 1 ? '#0f172a' : '#e2e8f0')}${text(80 + index * 400, 380, ['Starter', 'Yearly', 'Team'][index] ?? '', 20, '#0f172a', 600)}${step >= 3 ? text(80 + index * 400, 440, ['€9', '€90', '€290'][index] ?? '', 18, '#94a3b8', 400, 'text-decoration="line-through"') : ''}${text(step >= 3 ? 140 + index * 400 : 80 + index * 400, wrap && index === 1 ? 480 : 440, ['€6', '€63', '€203'][index] ?? '', 34, '#0f172a', 700)}${index === 1 ? `${box(330 + 64, 352, 92, 28, '#fde68a', 14)}${text(342 + 64, 372, '-30%', 14, '#7c2d12', 600)}` : ''}`).join('')}
${step >= 4 ? `${box(940, 84, 292, 56, '#0f172a', 10)}${text(966, 118, '✓  Deal applied', 15, '#fff', 500)}` : ''}`;
  },
  dashboard: (look) => {
    const bg = look.dark ? '#0b0f17' : '#ffffff';
    const ink = look.dark ? '#e5e7eb' : '#0f172a';
    const line = look.dark ? '#1f2937' : '#e2e8f0';
    const rows = Array.from({ length: 9 }, (_, index) => `${box(312, 196 + index * 56, 920, 1, line, 0)}${text(336, 230 + index * 56, `Order HS-${10400 + index * 7 + (look.step ?? 0)}`, 15, ink)}${text(760, 230 + index * 56, ['Paid', 'Shipped', 'Paid', 'Refunded'][index % 4] ?? '', 15, look.dark ? '#9ca3af' : '#475569')}`).join('');
    return `<rect y="0" width="1280" height="800" fill="${bg}"/>${box(0, 0, 264, 800, look.dark ? '#111827' : '#f8fafc', 0)}${text(32, 48, 'Admin', 20, ink, 700)}${['Orders', 'Products', 'Customers', 'Settings'].map((item, index) => text(32, 110 + index * 40, item, 15, ink)).join('')}${text(312, 140, 'Orders', 30, ink, 700)}${rows}`;
  },
};

/** The SVG of one frame of the mock's replays, from its look. */
export function framePicture(id: string): string | null {
  const match = /^fr_(\w+)_(\d{3})$/.exec(id);
  const line = match?.[1] ? SCRIPTS[match[1]]?.()[Number(match[2])] : undefined;
  if (!line || !('look' in line)) return null;
  const look = line.look;
  const body = PAGES[look.page](look);
  const chrome = look.page === 'dashboard' ? '' : header(look);
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800"><rect width="1280" height="800" fill="#ffffff"/>${chrome}${body}</svg>`;
}
