import { dayAndTime, plural, safeNext, safeRedirect, safeUrl, shortHash, slugify, timeAgo, timeUntil, usd } from './format';

const NOW = new Date('2026-11-04T12:00:00Z').getTime();

describe('safeUrl', () => {
  it.each(['javascript:alert(1)', 'data:text/html,<script>1</script>', 'vbscript:x', 'file:///etc/passwd', 'not a url', ''])(
    'refuses %s',
    (url) => expect(safeUrl(url)).toBeNull(),
  );
  it('allows http and https', () => {
    expect(safeUrl('https://example.com/a')).toBe('https://example.com/a');
    expect(safeUrl('http://example.com/')).toBe('http://example.com/');
  });
  it('handles a missing value', () => {
    expect(safeUrl(null)).toBeNull();
    expect(safeUrl(undefined)).toBeNull();
  });
});

describe('safeRedirect (security review 19)', () => {
  const origin = 'https://app.example';
  it.each(['javascript:alert(1)', 'data:text/html,x', 'http://billing.example/x', 'vbscript:x', '', null, 3])('refuses %j', (url) =>
    expect(safeRedirect(url, origin)).toBeNull(),
  );
  it('allows an https page and this panel', () => {
    expect(safeRedirect('https://billing.stripe.com/p/session/x', origin)).toBe('https://billing.stripe.com/p/session/x');
    expect(safeRedirect('/northwind-group/settings/billing', origin)).toBe('https://app.example/northwind-group/settings/billing');
  });
});

describe('safeNext', () => {
  it.each([
    'https://evil.example',
    '//evil.example',
    '/\\evil.example',
    '/\t/evil.example',
    '/\n/evil.example',
    '/a\\b',
    '/\u200b/evil.example',
    `/${'a'.repeat(512)}`,
    'javascript:alert(1)',
    'northwind-group',
    '',
    42,
    null,
  ])(
    'refuses %s',
    (next) => expect(safeNext(next)).toBeUndefined(),
  );
  it('allows a path on this site', () => {
    expect(safeNext('/northwind-group/needs-you')).toBe('/northwind-group/needs-you');
    expect(safeNext('/invite/abc')).toBe('/invite/abc');
  });
});

describe('time', () => {
  const at = (offset: number) => new Date(NOW + offset).toISOString();
  it('reads recent times plainly', () => {
    expect(timeAgo(at(-10_000), NOW)).toBe('Just now');
    expect(timeAgo(at(-5 * 60_000), NOW)).toBe('5m ago');
    expect(timeAgo(at(-3 * 3_600_000), NOW)).toBe('3h ago');
    expect(timeAgo(at(-2 * 86_400_000), NOW)).toBe('2d ago');
    expect(timeAgo(null, NOW)).toBe('Never');
  });
  it('says when something has expired', () => {
    expect(timeUntil(at(-1), NOW)).toBe('Expired');
    expect(timeUntil(at(90 * 60_000), NOW)).toBe('2h');
  });
  it('never shows a bare time for tomorrow', () => {
    const start = new Date(NOW);
    start.setHours(0, 0, 0, 0);
    const today = new Date(start.getTime() + 9 * 3_600_000).toISOString();
    const tomorrow = new Date(start.getTime() + 33 * 3_600_000).toISOString();
    expect(dayAndTime(today, NOW)).toMatch(/^Today /);
    expect(dayAndTime(tomorrow, NOW)).toMatch(/^Tomorrow /);
  });
});

it('formats the small things', () => {
  expect(usd(1.5)).toBe('$1.50');
  expect(plural(1, 'lane')).toBe('1 lane');
  expect(plural(2, 'lane')).toBe('2 lanes');
  expect(slugify('Aster Coffee & Co.')).toBe('aster-coffee-co');
  expect(shortHash('sha256:abcdef0123456789abcd')).toBe('sha256:abcdef012345');
});
