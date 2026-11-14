import type { Approval } from '@/api/types';
import { batchKind, kindOf, kindWords, stakeOf } from './kinds';
import { whereOf } from './words';

/** A setup approval as the node sends one: a template, an install, or a saved login, never a post. */
const setup = (effect: Approval['effect'], summary = 'Apply the social template to Biz A'): Approval => ({
  id: 'ap_setup',
  state: 'pending',
  effect,
  lane: 'biz-a/social',
  summary,
  content_hash: 'sha256:setup',
  preview: { text: '{"template":"social","lanes":["social"]}', image: null },
  requested_by: { kind: 'agent', id: 'claude-code' },
  created_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 60 * 60_000).toISOString(),
  site: null,
});

describe('a setup approval', () => {
  it('is its own kind, never "Post this?"', () => {
    for (const effect of ['install_overlay', 'install_base', 'service', 'secret'] as const) {
      expect(kindOf(setup(effect))).toBe('setup');
    }
    const words = kindWords(setup('install_overlay'), 'Biz A', 'Social');
    expect(words?.question).toBe('Allow this change on Biz A?');
    expect(words?.question).not.toMatch(/post/i);
  });

  it('is never batched as a post', () => {
    expect(batchKind(setup('install_overlay'))).toBe(false);
    expect(batchKind(setup('secret'))).toBe(false);
  });

  it('says what it is, in its own words, with a stake of its own', () => {
    expect(stakeOf(setup('secret')).key).toBe('secret');
    expect(stakeOf(setup('install_overlay')).key).toBe('tech');
  });

  it('reads the place from its lane, not from raw content', () => {
    const where = whereOf(setup('install_overlay'), 'Biz A');
    expect(where.business).toBe('Biz A');
  });
});
