/** Thin wrappers over the browser's WebAuthn JSON helpers. */
export function passkeysAvailable(): boolean {
  return typeof PublicKeyCredential !== 'undefined' && 'parseRequestOptionsFromJSON' in PublicKeyCredential;
}

export async function getPasskey(options: PublicKeyCredentialRequestOptionsJSON): Promise<unknown> {
  const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(options);
  const credential = await navigator.credentials.get({ publicKey });
  if (!credential) throw new Error('No passkey was selected.');
  return (credential as PublicKeyCredential).toJSON();
}

/**
 * The browser refuses a passkey prompt from a tab that is not in front. After a step-up the person
 * is often in the email tab, so the prompt waits until they come back to this one.
 */
function untilFocused(): Promise<void> {
  if (document.hasFocus()) return Promise.resolve();
  return new Promise((resolve) => {
    // A focus event is not promised on every way back to a tab, so check now and then as well.
    const check = () => {
      if (!document.hasFocus()) return;
      window.removeEventListener('focus', check);
      document.removeEventListener('visibilitychange', check);
      window.clearInterval(timer);
      resolve();
    };
    const timer = window.setInterval(check, 250);
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
  });
}

export async function createPasskey(options: PublicKeyCredentialCreationOptionsJSON): Promise<unknown> {
  const publicKey = PublicKeyCredential.parseCreationOptionsFromJSON(options);
  await untilFocused();
  const credential = await navigator.credentials.create({ publicKey });
  if (!credential) throw new Error('The passkey was not created.');
  return (credential as PublicKeyCredential).toJSON();
}

/** What to say when the device does not finish making a passkey. */
export function passkeyError(error: unknown): string | null {
  if (!(error instanceof DOMException)) return null;
  if (error.name === 'InvalidStateError') return 'This device already has a passkey for your account.';
  return 'The passkey was not added. Your device did not finish, or the prompt was closed. Try again.';
}
