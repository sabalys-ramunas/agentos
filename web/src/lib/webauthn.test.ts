import { describe, expect, it } from 'vitest';
import { passkeyError } from './webauthn';

describe('passkeyError', () => {
  it('says the device did not finish when the browser refuses or the prompt is closed', () => {
    expect(passkeyError(new DOMException('no focus', 'NotAllowedError'))).toMatch(/^The passkey was not added\./);
  });

  it('says when this device already holds a passkey for the account', () => {
    expect(passkeyError(new DOMException('excluded', 'InvalidStateError'))).toBe('This device already has a passkey for your account.');
  });

  it('leaves API errors to the usual wording', () => {
    expect(passkeyError(new Error('step_up_required'))).toBeNull();
  });
});
