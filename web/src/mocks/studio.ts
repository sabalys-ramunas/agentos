/**
 * The mock of Checks (`/studio`, openajar.api.web.studio): studio runs of Harbor Supply's web shop, one with
 * blockers and a failed screen, one clean, and a system check of the `harbor/shop` profile; screenshots and
 * crops as plain SVG wireframes; a baseline and a diff of the two checks. Studio missing: `?mock=no-studio`,
 * or `studioMissing` on the org (a test sets it).
 */
import { http, HttpResponse } from 'msw';
import type {
  BrandComponents,
  StudioBaseline,
  StudioDiff,
  StudioFinding,
  StudioFindingGroup,
  StudioMissing,
  StudioRun,
  StudioScreen,
} from '@/api/types';
import { scenario } from './scenario';
import type { Helpers } from './s3';
import type { OrgData } from './seed';

const MINUTE = 60_000;
export const SHOP_URL = 'https://shop.harborsupply.example/';
export const CAPTURE_BEFORE = 'cap_shopbefore01';
export const CAPTURE_AFTER = 'cap_shopafter002';
export const CAPTURE_SYSTEM = 'cap_shopsystem03';
export const RUN_BLOCKED = 'run_shopblock01';
export const RUN_CLEAN = 'run_shopclean01';
export const RUN_SYSTEM = 'run_shopsystem1';
export const RUN_PRUNED = 'run_shopcart001';
export const RUN_LONG = 'run_shopcheck01';
export const CAPTURE_PRUNED = 'cap_shopcart0001';
const CAPTURE_LONG = 'cap_shopcheck001';

export const STUDIO_MISSING: StudioMissing = {
  available: false,
  reason: 'Studio is not installed on this computer, so it has no checks. Add it with the command below and restart openajar, or run `openajar studio up` on a laptop.',
  command: 'uv sync --extra studio',
};

const screen = (state: string, width: number, theme: 'light' | 'dark', over: Partial<StudioScreen> = {}): StudioScreen => ({
  key: `${state}@${width}-${theme}`,
  width,
  theme,
  state,
  ok: true,
  error: null,
  image: true,
  ...over,
});

const FOUR = [screen('default', 360, 'light'), screen('default', 360, 'dark'), screen('default', 1280, 'light'), screen('default', 1280, 'dark')];

function finding(over: Partial<StudioFinding> & Pick<StudioFinding, 'id' | 'check' | 'severity'>): StudioFinding {
  return {
    confidence: 'measured',
    where: { selector: '.cart-summary .total', role: null, name: null },
    component: null,
    seen_at: [{ screen: 'default@360-light', count: 1 }],
    measured: {},
    expected: {},
    fix: null,
    why_heuristic: null,
    crop_screens: ['default@360-light'],
    ...over,
  };
}

const BLOCKED_GROUPS: StudioFindingGroup[] = [
  {
    component: 'CheckoutButton (src/checkout/CheckoutButton.tsx:18)',
    findings: [
      finding({
        id: 'f_1a2b3c4d5e', check: 'contrast.text', severity: 'blocker', component: 'CheckoutButton',
        where: { selector: 'button.checkout', role: 'button', name: 'Pay now' },
        seen_at: [{ screen: 'default@360-dark', count: 1 }, { screen: 'default@1280-dark', count: 1 }],
        measured: { ratio: 2.1, color: '#5b6b80', background: '#1c2430' }, expected: { min_ratio: 4.5 },
        fix: 'Use the text-on-accent token for the label in dark mode.', crop_screens: ['default@360-dark', 'default@1280-dark'],
        rects: { 'default@360-dark': { x: 16, y: 500, w: 328, h: 52 }, 'default@1280-dark': { x: 16, y: 1972, w: 1248, h: 52 } },
      }),
      finding({
        id: 'f_2b3c4d5e6f', check: 'target.size', severity: 'major', component: 'CheckoutButton',
        where: { selector: 'button.checkout .edit', role: 'button', name: 'Edit' },
        measured: { width: 28, height: 24 }, expected: { min_px: 44 }, fix: 'Give the edit control a 44 px hit area.',
        rects: { 'default@360-light': { x: 304, y: 514, w: 28, h: 24 } },
      }),
    ],
  },
  {
    component: 'ProductCard (src/catalog/ProductCard.tsx:7)',
    findings: [
      finding({
        id: 'f_3c4d5e6f7a', check: 'text.truncated', severity: 'blocker', component: 'ProductCard',
        where: { selector: '.product-card h3', role: 'heading', name: 'Stainless steel cleat, 6 inch, marine gr' },
        measured: { hidden: 0.42 }, expected: { min_hidden: 0 }, fix: 'Let the title wrap to two lines on phones.',
        rects: { 'default@360-light': { x: 26, y: 276, w: 110, h: 16 } },
      }),
      finding({
        id: 'f_4d5e6f7a8b', check: 'token.color', severity: 'major', component: 'ProductCard', confidence: 'heuristic',
        where: { selector: '.product-card .price', role: null, name: '€12.40' },
        measured: { value: '#1f60ad', category: 'color', delta_eok: 0.002, match: 'near', count: 8 },
        expected: { token: 'color.accent', value: '#1f5fad', css_var: '--color-accent' }, fix: 'Use var(--color-accent).',
        rects: { 'default@360-light': { x: 26, y: 300, w: 48, h: 16 } },
        why_heuristic: 'The value is one step from a token; it may be a copy of it.',
      }),
    ],
  },
  {
    component: 'page',
    grouped_by: 'no component marker, landmark or named part: the whole page',
    findings: [
      finding({
        id: 'f_5e6f7a8b9c', check: 'layout.overflow', severity: 'major', where: { selector: 'main', role: 'main', name: null },
        measured: { overflow_px: 18 }, fix: 'The filter row is wider than the phone screen: let it scroll on its own.',
        rects: { 'default@360-light': { x: 0, y: 58, w: 360, h: 56 } },
      }),
      finding({
        id: 'f_6f7a8b9c0d', check: 'names.missing', severity: 'minor', where: { selector: 'a.logo', role: 'link', name: null },
        fix: 'Give the logo link a name: Harbor Supply home.', crop_screens: [],
      }),
      finding({
        id: 'f_7a8b9c0d1e', check: 'health.images', severity: 'info', where: { selector: 'img.hero', role: 'img', name: 'Sailing boat' },
        measured: { oversize: 3.2 }, fix: 'Serve the hero image at the size it is shown.', crop_screens: [],
      }),
    ],
  },
];

function row(now: number, id: string, kind: 'check' | 'system', capture: string, minutes: number, counts: StudioRun['counts'], over: Partial<StudioRun> = {}): StudioRun {
  return {
    id,
    kind,
    url: SHOP_URL,
    profile: 'harbor/shop',
    created_at: new Date(now - minutes * MINUTE).toISOString(),
    capture,
    counts,
    system_share: null,
    screens: 4,
    screens_failed: 0,
    verdict: '',
    groups: [],
    screen_list: FOUR,
    problems: [],
    system: null,
    images_gone: false,
    ...over,
  };
}

export function seedStudio(now = Date.now()): StudioRun[] {
  return [
    row(now, RUN_CLEAN, 'check', CAPTURE_AFTER, 12, { blocker: 0, major: 0, minor: 0, info: 0, total: 0 }, {
      verdict: 'Clean: no findings on 4 screens',
      system_share: 0.94,
    }),
    row(now, RUN_SYSTEM, 'system', CAPTURE_SYSTEM, 30, { blocker: 0, major: 1, minor: 2, info: 0, total: 3 }, {
      verdict: 'Not clean: 3 findings (1 major, 2 minor) on 2 screens; design system checked against profile harbor/shop (24 tokens): 3 findings',
      screens: 2,
      screen_list: [screen('default', 1280, 'light'), screen('default', 1280, 'dark')],
      system_share: 0.81,
      system: { on_tokens: { color: 0.86, radius: 0.92, space: 0.74, type: 0.72 }, hand_rolled_controls: 3, controls_in_components: 0.88, probe: { token: 212, derived: 31, look_alike: 9 } },
      groups: [
        {
          component: 'ProductCard',
          findings: [
            finding({ id: 'f_8b9c0d1e2f', check: 'token.hardcoded', severity: 'major', component: 'ProductCard', where: { selector: '.product-card', role: null, name: null }, measured: { value: '7px', category: 'radius', properties: ['border-radius'], delta_px: 1, match: 'exact', count: 24 }, expected: { token: 'radius.md', value: '8px', css_var: '--radius-md' }, fix: 'Use var(--radius-md).', seen_at: [{ screen: 'default@1280-light', count: 24 }], crop_screens: ['default@1280-light'], rects: { 'default@1280-light': { x: 16, y: 140, w: 300, h: 390 } } }),
            finding({ id: 'f_9c0d1e2f3a', check: 'token.hardcoded', severity: 'minor', component: 'ProductCard', where: { selector: '.product-card .price', role: null, name: '€12.40' }, measured: { value: '#1f60ad', category: 'color', properties: ['color'], delta_eok: 0.002, match: 'exact', count: 24 }, expected: { token: 'color.accent', value: '#1f5fad', css_var: '--color-accent' }, fix: 'Use var(--color-accent).', seen_at: [{ screen: 'default@1280-light', count: 24 }], crop_screens: ['default@1280-light'], rects: { 'default@1280-light': { x: 26, y: 451, w: 90, h: 16 } } }),
          ],
        },
        {
          component: 'page',
          findings: [finding({ id: 'f_0d1e2f3a4b', check: 'component.hand_rolled', severity: 'minor', where: { selector: 'form.newsletter button', role: 'button', name: 'Sign up' }, measured: { control: '<button>', count: 1 }, expected: { rule: 'every control comes from a known component', components: 'Badge, Button, CheckoutButton, PriceTag, ProductCard' }, why_heuristic: 'a control outside every known component is taken as hand-built; a component the rules do not list looks the same', confidence: 'heuristic', fix: 'Use the Button component for the sign-up button.', seen_at: [{ screen: 'default@1280-light', count: 1 }], crop_screens: ['default@1280-light'], rects: { 'default@1280-light': { x: 16, y: 1972, w: 1248, h: 52 } } })],
        },
      ],
    }),
    row(now, RUN_BLOCKED, 'check', CAPTURE_BEFORE, 95, { blocker: 2, major: 3, minor: 1, info: 1, total: 7 }, {
      verdict: 'Not clean: 7 findings (2 blocker, 3 major, 1 minor, 1 info) on 3 of 4 screens',
      screens_failed: 1,
      system_share: 0.78,
      system: { on_tokens: { color: 0.81, radius: 0.9, space: 0.7, type: 0.72 }, hand_rolled_controls: 2, controls_in_components: 0.9, probe: { token: 180, derived: 20, look_alike: 6 } },
      groups: BLOCKED_GROUPS,
      screen_list: [...FOUR.slice(0, 3), screen('default', 1280, 'dark', { ok: false, image: false, error: 'The page did not finish loading in 30 s.' })],
      problems: ['default@1280-dark failed: The page did not finish loading in 30 s.', 'fonts not pinned: a change in text wrapping can come from the machine, not the code'],
    }),
    // An older check of the cart whose pictures studio has let go: its findings still read without them.
    row(now, RUN_PRUNED, 'check', CAPTURE_PRUNED, 3 * 24 * 60, { blocker: 0, major: 1, minor: 1, info: 0, total: 2 }, {
      url: `${SHOP_URL}cart`,
      verdict: 'Not clean: 2 findings (1 major, 1 minor) on 4 screens',
      system_share: 0.83,
      images_gone: true,
      screen_list: FOUR.map((item) => ({ ...item, image: false })),
      groups: [
        {
          component: 'CartRow (src/cart/CartRow.tsx:12)',
          findings: [
            finding({
              id: 'f_aa1b2c3d4e', check: 'target.size', severity: 'major', component: 'CartRow',
              where: { selector: '.cart-row button.remove', role: 'button', name: 'Remove' },
              measured: { width: 20, height: 20 }, expected: { min_px: 24 }, fix: 'Give the remove control a 24 px hit area at least.',
              seen_at: [{ screen: 'default@360-light', count: 3 }, { screen: 'default@360-dark', count: 3 }],
            }),
          ],
        },
        {
          component: 'page',
          findings: [
            finding({
              id: 'f_bb2c3d4e5f', check: 'names.missing', severity: 'minor', where: { selector: 'a.continue', role: 'link', name: null },
              fix: 'Name the arrow link: Continue shopping.', crop_screens: [],
            }),
          ],
        },
      ],
    }),
    // A long address with a query, checked without a product profile, and clean.
    row(now, RUN_LONG, 'check', CAPTURE_LONG, 5 * 24 * 60, { blocker: 0, major: 0, minor: 0, info: 0, total: 0 }, {
      url: `${SHOP_URL}checkout/delivery-and-payment?step=2&coupon=SPRING-SAILING-2026&from=email-campaign-november`,
      profile: null,
      verdict: 'Clean: no findings on 4 screens',
      system_share: null,
    }),
  ];
}

const DIFF: StudioDiff = {
  before: CAPTURE_BEFORE,
  after: CAPTURE_AFTER,
  verdict: '2 of 4 screens changed: 3 causes.',
  screens: [
    {
      screen: 'default@360-light',
      status: 'changed',
      lines: [
        'The product card title "Stainless steel cleat, 6 inch, marine grade" wraps to two lines: the card is 22 px taller.',
        'The checkout button moved down 22 px, pushed by the card above it.',
      ],
      pixels_changed: 0.083,
      unexplained: [],
      changes: [
        { kind: 'resized', key: 'catalog/product-card[1]', component: 'ProductCard' },
        { kind: 'moved', key: 'checkout/pay', component: 'CheckoutButton' },
      ],
    },
    {
      screen: 'default@360-dark',
      status: 'changed',
      lines: ['The checkout button label changed colour from #5b6b80 to color.text-on-accent (#ffffff).'],
      pixels_changed: 0.004,
      unexplained: [{ rect: { x: 0, y: 64, w: 360, h: 180 }, share: 0.06 }],
      changes: [{ kind: 'restyled', key: 'checkout/pay', component: 'CheckoutButton' }],
    },
    { screen: 'default@1280-light', status: 'same', lines: [], pixels_changed: 0, unexplained: [], changes: [] },
    { screen: 'default@1280-dark', status: 'skipped', reason: 'the before capture has no image of this screen: it failed', lines: [], pixels_changed: 0, unexplained: [], changes: [] },
  ],
};

const COMPONENTS = (now: number): BrandComponents => ({
  profile: 'harbor/shop',
  run: RUN_SYSTEM,
  url: SHOP_URL,
  checked_at: new Date(now - 30 * MINUTE).toISOString(),
  components: [
    { name: 'ProductCard', uses: 48, screens: 2, findings: 2 },
    { name: 'Button', uses: 31, screens: 2, findings: 0 },
    { name: 'PriceTag', uses: 24, screens: 2, findings: 0 },
    { name: 'CheckoutButton', uses: 2, screens: 2, findings: 0 },
    { name: 'SearchField', uses: 2, screens: 2, findings: 0 },
    { name: 'Badge', uses: 0, screens: 0, findings: 0 },
  ],
  hand_rolled: 3,
  look_alikes: { token: 212, derived: 31, look_alike: 9 },
  look_alike_findings: [],
  on_tokens: { color: 0.86, radius: 0.92, space: 0.74, type: 0.72 },
});

/** A wireframe of the shop at a screen's width and theme. No real site is drawn. */
function shopSvg(width: number, dark: boolean): string {
  const h = Math.round(width * 1.6);
  const bg = dark ? '#121417' : '#ffffff';
  const card = dark ? '#1c2026' : '#f3f4f6';
  const ink = dark ? '#3a414b' : '#d6d9de';
  const cols = width >= 1000 ? 4 : 2;
  const pad = 16;
  const cw = (width - pad * (cols + 1)) / cols;
  const cards = Array.from({ length: cols * 2 }, (_, i) => {
    const x = pad + (i % cols) * (cw + pad);
    const y = 140 + Math.floor(i / cols) * (cw * 1.3 + pad);
    return `<rect x="${x}" y="${y}" width="${cw}" height="${cw * 1.3}" rx="8" fill="${card}"/><rect x="${x + 10}" y="${y + cw * 0.9}" width="${cw * 0.7}" height="8" rx="4" fill="${ink}"/><rect x="${x + 10}" y="${y + cw * 1.05}" width="${cw * 0.3}" height="8" rx="4" fill="#1f5fad"/>`;
  }).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${h}" viewBox="0 0 ${width} ${h}"><rect width="${width}" height="${h}" fill="${bg}"/><rect x="${pad}" y="24" width="120" height="16" rx="4" fill="${ink}"/><rect x="${pad}" y="64" width="${width - pad * 2}" height="44" rx="8" fill="${card}"/>${cards}<rect x="${pad}" y="${h - 76}" width="${width - pad * 2}" height="52" rx="10" fill="#1f5fad"/></svg>`;
}

function cropSvg(dark: boolean): string {
  const bg = dark ? '#121417' : '#ffffff';
  return `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="220" viewBox="0 0 400 220"><rect width="400" height="220" fill="${bg}"/><rect x="40" y="60" width="320" height="100" rx="8" fill="${dark ? '#1c2026' : '#f3f4f6'}"/><rect x="36" y="56" width="328" height="108" rx="10" fill="none" stroke="#e5484d" stroke-width="3"/></svg>`;
}

const svg = (body: string) =>
  new HttpResponse(body, { headers: { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'private, max-age=86400, immutable' } });

export function studioHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const runsOf = (record: OrgData): StudioRun[] => {
    record.studioRuns ??= record.org.slug === 'northwind-group' ? seedStudio() : [];
    return record.studioRuns;
  };
  const missing = (record: OrgData) => record.studioMissing === true || scenario.has('no-studio');
  const unavailable = () => failure(503, 'unavailable', 'Studio is not installed on this computer. Add it with `uv sync --extra studio` and restart openajar.');
  const notFound = (what: string) => failure(404, 'not_found', what);

  return [
    http.get(
      '/api/orgs/:org/studio/runs',
      org((record, { request }) => {
        if (missing(record)) return HttpResponse.json(STUDIO_MISSING);
        const query = new URL(request.url).searchParams;
        const url = query.get('url');
        const profile = query.get('profile');
        const runs = runsOf(record)
          .filter((run) => (!url || run.url === url) && (!profile || run.profile === profile))
          .map(({ verdict: _v, groups: _g, screen_list: _s, problems: _p, system: _y, images_gone: _i, ...rest }) => rest);
        return HttpResponse.json({ available: true, runs });
      }),
    ),
    http.get(
      '/api/orgs/:org/studio/runs/:id',
      org((record, { params }) => {
        if (missing(record)) return unavailable();
        const run = runsOf(record).find((item) => item.id === params.id);
        return run ? HttpResponse.json(run) : notFound('No such check: it is older than the kept runs, or the id is wrong.');
      }),
    ),
    http.get(
      '/api/orgs/:org/studio/runs/:id/findings/:fid/crop',
      org((record, { params, request }) => {
        if (missing(record)) return unavailable();
        const run = runsOf(record).find((item) => item.id === params.id);
        const found = run?.groups.flatMap((group) => group.findings).find((item) => item.id === params.fid);
        if (!found || found.crop_screens.length === 0) return notFound('This finding has no place on that screen to show.');
        const screenKey = new URL(request.url).searchParams.get('screen') ?? found.crop_screens[0] ?? '';
        return svg(cropSvg(screenKey.endsWith('-dark')));
      }),
    ),
    http.get(
      '/api/orgs/:org/studio/captures/:id/screens/:screen',
      org((record, { params }) => {
        if (missing(record)) return unavailable();
        const key = String(params.screen);
        const match = /^[^@]+@(\d+)-(light|dark)$/.exec(key);
        if (!match) return notFound('This capture has no such screen.');
        if (params.id === CAPTURE_BEFORE && key === 'default@1280-dark') return notFound('This screen has no image: it failed.');
        if (params.id === CAPTURE_PRUNED) return notFound('The picture of this screen is no longer kept.');
        return svg(shopSvg(Number(match[1]), match[2] === 'dark'));
      }),
    ),
    http.get(
      '/api/orgs/:org/studio/baseline',
      org((record, { request }) => {
        if (missing(record)) return unavailable();
        const query = new URL(request.url).searchParams;
        const url = query.get('url') ?? '';
        const profile = query.get('profile');
        if (!url) return failure(400, 'invalid', 'Name the page: url.');
        const kept = url === SHOP_URL && record.org.slug === 'northwind-group';
        const body: StudioBaseline = kept
          ? { url, profile, capture: CAPTURE_BEFORE, created_at: new Date(Date.now() - 95 * MINUTE).toISOString(), screens: FOUR }
          : { url, profile, capture: null, created_at: null, screens: [] };
        return HttpResponse.json(body);
      }),
    ),
    http.get(
      '/api/orgs/:org/studio/diff',
      org((record, { request }) => {
        if (missing(record)) return unavailable();
        const query = new URL(request.url).searchParams;
        const before = query.get('before') ?? '';
        const after = query.get('after') ?? '';
        if (!/^cap_[0-9a-z]{6,40}$/.test(before) || !/^cap_[0-9a-z]{6,40}$/.test(after)) {
          return failure(400, 'invalid', 'before and after are capture ids, like cap_k3x9q2m4ab.');
        }
        if (before === after) return HttpResponse.json({ ...DIFF, before, after, verdict: 'No change on 4 of 4 screens.', screens: DIFF.screens.map((s) => ({ ...s, status: 'same', lines: [], changes: [], unexplained: [], pixels_changed: 0 })) });
        return HttpResponse.json({ ...DIFF, before, after });
      }),
    ),
    http.get(
      '/api/orgs/:org/studio/profiles/:ws/:id/components',
      org((record, { params }) => {
        if (missing(record)) return HttpResponse.json(STUDIO_MISSING);
        const ref = `${String(params.ws)}/${String(params.id)}`;
        if (ref === 'harbor/shop' && record.org.slug === 'northwind-group') {
          const body = COMPONENTS(Date.now());
          const system = runsOf(record).find((run) => run.id === RUN_SYSTEM);
          body.look_alike_findings = system?.groups.flatMap((group) => group.findings).filter((item) => item.check === 'token.hardcoded') ?? [];
          return HttpResponse.json(body);
        }
        const empty: BrandComponents = { profile: ref, run: null, url: null, checked_at: null, components: [], hand_rolled: 0, look_alikes: null, look_alike_findings: [], on_tokens: {} };
        return HttpResponse.json(empty);
      }),
    ),
  ];
}
