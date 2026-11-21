import type { StudioFinding, StudioRunRow } from '@/api/types';
import { summary } from './checks-page';
import { atLeast, checkAgain, countsByScreen, findingTitle, forAgent, lookAlikes, numbered, pagePath, paintable, recordLine, runStatus, screenOfKey, verdictWords, worstByScreen } from './model';

const finding = (over: Partial<StudioFinding> & Pick<StudioFinding, 'id' | 'check' | 'severity'>): StudioFinding => ({
  confidence: 'measured',
  where: { selector: 'button.pay', role: 'button', name: 'Pay now' },
  component: null,
  seen_at: [{ screen: 'default@360-light', count: 1 }],
  measured: {},
  expected: {},
  fix: null,
  why_heuristic: null,
  crop_screens: [],
  ...over,
});

const row = (over: Partial<StudioRunRow> = {}): StudioRunRow => ({
  id: 'run_1',
  kind: 'check',
  url: 'https://shop.example/cart',
  profile: 'acme/shop',
  created_at: '2026-11-13T10:00:00Z',
  capture: 'cap_aaaaaaaa',
  counts: { blocker: 0, major: 0, minor: 0, info: 0, total: 0 },
  system_share: null,
  screens: 4,
  screens_failed: 0,
  ...over,
});

describe('a check in plain words', () => {
  it('numbers findings from 1 in the order the node groups them, and filters by severity', () => {
    const all = numbered([
      { component: 'Button', findings: [finding({ id: 'f_a', check: 'contrast.text', severity: 'blocker' }), finding({ id: 'f_b', check: 'target.size', severity: 'minor' })] },
      { component: 'page', findings: [finding({ id: 'f_c', check: 'names.missing', severity: 'major' })] },
    ]);
    expect(all.map((item) => [item.n, item.id, item.group])).toEqual([
      [1, 'f_a', 'Button'],
      [2, 'f_b', 'Button'],
      [3, 'f_c', 'page'],
    ]);
    expect(atLeast(all, 'major').map((item) => item.id)).toEqual(['f_a', 'f_c']);
  });

  it('reads screens from their keys and counts findings on each', () => {
    expect(screenOfKey('default@1280-dark')).toEqual({ width: 1280, theme: 'dark', state: 'default' });
    expect(screenOfKey('nonsense')).toBeNull();
    const counts = countsByScreen([
      finding({ id: 'f_a', check: 'x', severity: 'minor', rects: { 'default@360-dark': { x: 1, y: 1, w: 1, h: 1 } } }),
      finding({ id: 'f_b', check: 'x', severity: 'minor' }),
    ]);
    expect(counts.get('default@360-light')).toBe(2);
    expect(counts.get('default@360-dark')).toBe(1);
  });

  it('gives the agent the id, the check, where, measured, expected and the fix', () => {
    const [item] = numbered([
      {
        component: 'Button',
        findings: [
          finding({ id: 'f_1a2b3c4d5e', check: 'contrast.text', severity: 'blocker', measured: { ratio: 2.1, selectors: ['a', 'b'] }, expected: { min_ratio: 4.5 }, fix: 'Use the on-accent token.' }),
        ],
      },
    ]);
    const text = forAgent({ id: 'run_9', url: 'https://shop.example/' }, item!);
    expect(text).toContain('f_1a2b3c4d5e');
    expect(text).toContain('Check: contrast.text (blocker, measured)');
    expect(text).toContain('Where: button.pay button "Pay now"');
    expect(text).toContain('Measured: ratio 2.1');
    expect(text).not.toContain('selectors');
    expect(text).toContain('Expected: min ratio 4.5');
    expect(text).toContain('Fix: Use the on-accent token.');
    expect(findingTitle(item!)).toBe('Text is hard to read: "Pay now"');
    expect(recordLine({ value: '7px', delta_px: 1, method: 'probe' })).toBe('value 7px, delta px 1');
  });

  it('says a run in one status, worst first', () => {
    expect(runStatus(row({ counts: { blocker: 2, major: 1, minor: 0, info: 0, total: 3 } }))).toEqual({ label: '2 blockers', tone: 'bad' });
    expect(runStatus(row({ counts: { blocker: 0, major: 0, minor: 0, info: 1, total: 1 } }))).toEqual({ label: 'Clean', tone: 'ok' });
    expect(runStatus(row({ screens_failed: 4 })).label).toBe('Did not run');
    expect(checkAgain(row())).toBe('ui_check url=https://shop.example/cart profile=acme/shop since=run_1');
    expect(pagePath('https://shop.example/a?b=1')).toEqual({ path: '/a?b=1', host: 'shop.example' });
  });

  it('finds values that only look like a token, with the nearest token and how far', () => {
    const near = lookAlikes([
      finding({ id: 'f_a', check: 'token.hardcoded', severity: 'minor', measured: { value: '7px', category: 'radius', delta_px: 1, count: 24 }, expected: { token: 'radius.md', value: '8px' } }),
      finding({ id: 'f_b', check: 'token.color', severity: 'major', measured: { value: '#123456', match: 'off' } }),
    ]);
    expect(near).toHaveLength(1);
    expect(near[0]).toMatchObject({ value: '7px', token: 'radius.md', tokenValue: '8px', distance: '1 px off', count: 24 });
  });

  it('paints only a colour value, never anything else from the data', () => {
    expect(paintable('rgb(1, 2, 3)')).toBe('rgb(1, 2, 3)');
    expect(paintable('url(https://evil.example/x.png)')).toBeNull();
    expect(paintable('{color.paper}')).toBeNull();
    expect(paintable(null)).toBeNull();
  });

  it('sums the newest check of each page in one sentence', () => {
    expect(summary([])).toBe('No page has been checked here yet.');
    const text = summary([
      row({ id: 'r3', url: 'https://shop.example/', counts: { blocker: 1, major: 1, minor: 0, info: 0, total: 2 }, system_share: 0.8 }),
      row({ id: 'r2', url: 'https://shop.example/', system_share: 0.5 }),
      row({ id: 'r1', url: 'https://shop.example/cart', system_share: 0.9 }),
    ]);
    expect(text).toBe('3 checks of 2 pages. The newest checks have 2 open findings on 1 page, 1 blocker among them, and 85% of values are on the design system.');
  });
});

describe('a check in the page\'s words', () => {
  it('says blockers in the plural and "info" as "to know"', () => {
    expect(verdictWords('Not clean: 7 findings (2 blocker, 3 major, 1 minor, 1 info) on 3 of 4 screens')).toBe(
      'Not clean: 7 findings (2 blockers, 3 major, 1 minor, 1 to know) on 3 of 4 screens',
    );
    expect(verdictWords('Not clean: 1 finding (1 blocker)')).toBe('Not clean: 1 finding (1 blocker)');
  });

  it('knows the worst finding on each screen', () => {
    const worst = worstByScreen([
      finding({ id: 'a', check: 'names.missing', severity: 'minor' }),
      finding({ id: 'b', check: 'contrast.text', severity: 'blocker' }),
      finding({ id: 'c', check: 'target.size', severity: 'major', seen_at: [{ screen: 'default@1280-light', count: 1 }] }),
    ]);
    expect(worst.get('default@360-light')).toBe('blocker');
    expect(worst.get('default@1280-light')).toBe('major');
  });
});
