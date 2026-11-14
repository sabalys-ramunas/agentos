import { viewOnlyWords } from './parts';

describe('the view-only line under a live account', () => {
  it('says who holds the account, when it is not this person', () => {
    expect(viewOnlyWords('Mara Lindqvist', true)).toBe('View only. Mara Lindqvist has control');
    expect(viewOnlyWords('The owner', false)).toBe('View only. The owner has control');
  });

  it('offers the take-over to the owner alone', () => {
    expect(viewOnlyWords(null, true)).toBe('View only. Take over to type or click');
    // An operator or a viewer cannot take over: they are told who can, not told to take over
    expect(viewOnlyWords(null, false)).toBe('View only. Only the owner can take over this account.');
  });
});
