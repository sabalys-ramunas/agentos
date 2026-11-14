import { bytesText, keptSentence, recordSentence, waitingSentence, waitsForLook } from './memory-words';

describe('what waits for a look', () => {
  it('counts an agent note or outside text nobody has reviewed, and nothing else', () => {
    expect(waitsForLook({ trust: 'agent', state: 'live', reviewed: false })).toBe(true);
    expect(waitsForLook({ trust: 'untrusted', state: 'live', reviewed: false })).toBe(true);
    expect(waitsForLook({ trust: 'agent', state: 'live', reviewed: true })).toBe(false);
    expect(waitsForLook({ trust: 'owner', state: 'live', reviewed: false })).toBe(false);
    expect(waitsForLook({ trust: 'node', state: 'live', reviewed: false })).toBe(false);
    expect(waitsForLook({ trust: 'agent', state: 'forgotten', reviewed: false })).toBe(false);
  });

  it('does not claim a search hit waits, since a hit does not say whether it was reviewed', () => {
    expect(waitsForLook({ trust: 'agent', state: 'live', reviewed: undefined })).toBe(false);
    expect(waitsForLook({ trust: 'agent', state: 'live', reviewed: null })).toBe(false);
  });

  it('says how many, in plain words', () => {
    expect(waitingSentence(0)).toBe('Nothing waits for a look.');
    expect(waitingSentence(1)).toBe('1 thing waits for a look.');
    expect(waitingSentence(4)).toBe('4 things wait for a look.');
  });
});

describe('the room of a workspace', () => {
  it('reads its bytes and its entries the way a person counts them', () => {
    expect(bytesText(314)).toBe('314 bytes');
    expect(bytesText(2048)).toBe('2 KB');
    expect(bytesText(50_000_000)).toBe('47.7 MB');
    const row = { workspace: 'aster', live_entries: 3, live_entries_cap: 500, content_bytes: 0, content_bytes_cap: 0, waiting: 0, chain_ok: true, forgotten: 0 };
    expect(keptSentence(row)).toBe('3 of 500 entries kept');
    expect(recordSentence(row)).toBe('Record checks out');
    expect(recordSentence({ ...row, chain_ok: false })).toBe('Changed outside agentos');
  });
});
