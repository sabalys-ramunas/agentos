import { describe, expect, it } from 'vitest';
import { ApiError } from '@/api/http';
import { describeError } from './errors';

describe('describeError', () => {
  it('says a refusal from the computer in plain words, not its command', () => {
    const error = new ApiError(401, 'unauthorized', 'Sign in first. Run `agentos panel login` on this computer.');
    expect(describeError(error)).toBe('Sign in again with a new code from this computer.');
  });

  it('says a failed push in plain words, not the raw reason', () => {
    const error = new ApiError(400, 'invalid', 'ntfy unreachable: ConnectError');
    expect(describeError(error)).toBe('The ntfy service could not be reached from this computer. Try again in a moment.');
  });

  it('says a busy account in plain words', () => {
    const error = new ApiError(409, 'invalid', 'lane biz-a/social is warm; close it first');
    expect(describeError(error)).toBe('This account is open right now. Close it first, then try again.');
  });

  it('keeps a message it does not know as the computer wrote it', () => {
    const error = new ApiError(400, 'invalid', 'Pick a workspace first.');
    expect(describeError(error)).toBe('Pick a workspace first.');
  });

  it('keeps the calm line for a server error', () => {
    const error = new ApiError(500, 'internal', 'Traceback (most recent call last)');
    expect(describeError(error)).toBe('The service did not answer. Nothing was changed.');
  });
});
