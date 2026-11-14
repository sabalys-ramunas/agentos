import { http, HttpResponse } from 'msw';
import { server } from './setup';

/**
 * Makes the app a computer's: it signs in with a code, has no magic link, and holds the things only a
 * computer does (a passkey check, a watch link, a mailbox, the vault). The mock answers as the cloud
 * does by default, so a test of one of those calls this first.
 */
export function asComputer() {
  server.use(http.get('/api/auth/providers', () => HttpResponse.json({ magic_link: false, google: false, passkey: true, code: true })));
}
