import type { TraceEvent } from '@/api/types';
import { activityCsv, csvCell } from './csv';

describe('activity CSV (security review 11)', () => {
  it.each(['=HYPERLINK("https://evil.example","x")', '+1+1', '-2+3', '@SUM(A1)', '\tcmd', '\rcmd'])(
    'makes %j plain text, so a spreadsheet does not run it',
    (value) => expect(csvCell(value)).toMatch(/^"'/),
  );

  it('leaves ordinary text alone and quotes it', () => {
    expect(csvCell('Posted the launch note')).toBe('"Posted the launch note"');
    expect(csvCell('say "hi"')).toBe('"say ""hi"""');
    expect(csvCell(null)).toBe('""');
    expect(csvCell(0.04)).toBe('"0.04"');
  });

  it('neutralises every column a node or agent controls', () => {
    const row = {
      at: '2026-11-04T10:00:00Z',
      status: 'failed',
      lane: '=lane',
      principal: '@agent',
      effect: 'commit',
      cost: { usd: 0.01, tokens: 1 },
      summary: '=cmd|" /C calc"!A0',
    } as unknown as TraceEvent;
    const [, line] = activityCsv([row]).split('\n');
    expect(line).toContain(`"'=lane"`);
    expect(line).toContain(`"'@agent"`);
    expect(line).toContain(`"'=cmd|"" /C calc""!A0"`);
  });
});
