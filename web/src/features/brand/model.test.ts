import type { BrandProfile } from '@/api/types';
import { drawnButton, guidelineChanges, pairColours, refOf, resolver, tokenChanges } from './model';

const profile = (tokens: { name: string; type: string | null; value: string }[]): BrandProfile => ({
  ref: 'acme/shop',
  workspace: 'acme',
  profile: 'shop',
  kind: 'product',
  display_name: 'Acme shop',
  headline: null,
  version: 1,
  state: 'approved',
  draft_waiting: null,
  website: null,
  created_at: '2026-11-01T00:00:00Z',
  decided_at: null,
  brand: { tokens, fonts: [{ family: 'Inter', role: 'body', weights: [400] }], logo: null, components: [], theme: null, widths: [], engine: null },
  guidelines: [{ id: 'contrast', text: 'Text is easy to read', check: 'contrast.text', threshold: 4.5, advisory: false }],
  references: [],
  environments: [],
  draft: null,
});

describe('a brand as data', () => {
  it('follows aliases to their values', () => {
    const resolve = resolver([
      { name: 'color.paper', type: 'color', value: 'rgb(255, 255, 255)' },
      { name: 'color.on-accent', type: 'color', value: '{color.paper}' },
    ]);
    expect(resolve('{color.on-accent}')).toBe('rgb(255, 255, 255)');
    expect(resolve('{color.missing}')).toBe('{color.missing}');
  });

  it('pairs colours light and dark by name', () => {
    const { pairs, single } = pairColours([
      { name: 'color.surface.light', type: 'color', value: 'rgb(1, 1, 1)' },
      { name: 'color.surface.dark', type: 'color', value: 'rgb(2, 2, 2)' },
      { name: 'color.accent', type: 'color', value: 'rgb(3, 3, 3)' },
    ]);
    expect(pairs).toEqual([{ name: 'color.surface', light: expect.objectContaining({ value: 'rgb(1, 1, 1)' }), dark: expect.objectContaining({ value: 'rgb(2, 2, 2)' }) }]);
    expect(single.map((token) => token.name)).toEqual(['color.accent']);
  });

  it('says what a draft changes, token by token and guideline by guideline', () => {
    const now = profile([{ name: 'color.accent', type: 'color', value: 'rgb(0, 0, 255)' }, { name: 'space.1', type: 'dimension', value: '4px' }]);
    const draft = {
      version: 2,
      created_at: '2026-11-13T00:00:00Z',
      created_by: 'claude-code',
      reason: null,
      diff: [],
      tokens: [
        { name: 'color.accent', type: 'color', value: 'rgb(0, 128, 0)' },
        { name: 'color.accent-strong', type: 'color', value: 'rgb(0, 64, 0)' },
      ],
      guidelines: [{ id: 'one-accent', text: 'One accent per screen', check: null, threshold: null, advisory: true }],
    };
    expect(tokenChanges(now, draft)).toEqual([
      { name: 'color.accent', before: 'rgb(0, 0, 255)', after: 'rgb(0, 128, 0)', colour: true },
      { name: 'color.accent-strong', before: null, after: 'rgb(0, 64, 0)', colour: true },
      { name: 'space.1', before: '4px', after: null, colour: false },
    ]);
    expect(guidelineChanges(now, draft)!.map((change) => [change.id, change.kind])).toEqual([
      ['one-accent', 'added'],
      ['contrast', 'removed'],
    ]);
    expect(tokenChanges(now, { ...draft, tokens: undefined })).toBeNull();
  });

  it('draws a Button only from a fill and a text colour the profile names', () => {
    expect(drawnButton(profile([{ name: 'color.accent', type: 'color', value: 'rgb(0, 0, 255)' }]))).toBeNull();
    const button = drawnButton(
      profile([
        { name: 'color.accent', type: 'color', value: 'rgb(0, 0, 255)' },
        { name: 'color.text-on-accent', type: 'color', value: 'rgb(255, 255, 255)' },
        { name: 'radius.md', type: 'dimension', value: '8px' },
        { name: 'space.2', type: 'dimension', value: '8px' },
        { name: 'space.4', type: 'dimension', value: '16px' },
      ]),
    );
    expect(button).toMatchObject({ background: 'rgb(0, 0, 255)', color: 'rgb(255, 255, 255)', radius: '8px', paddingY: '8px', paddingX: '16px', font: 'Inter' });
    expect(button!.from).toEqual(['color.accent', 'color.text-on-accent', 'radius.md', 'space.2', 'space.4', 'brand.fonts']);
  });

  it('reads a profile address with a slash or a tilde', () => {
    expect(refOf('acme/shop')).toBe('acme/shop');
    expect(refOf('acme~shop')).toBe('acme/shop');
  });
});
