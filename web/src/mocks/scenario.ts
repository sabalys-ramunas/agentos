/**
 * Demo and test scenarios, set once with ?mock=... and kept for the tab. Screens stay
 * unaware: this only changes what the mock backend answers.
 *   still    no background live changes (stable screenshots and tests)
 *   loading  org data never resolves (skeletons)
 *   error    org data answers 500 (error states)
 *   all-good nothing waits and every account is fine (Home's calm state)
 *   log-changed the log check says the log was changed after it was written (Proof)
 *   computer the app is on a computer, not hosted: it signs in with a code and has what only a computer has
 *   one-machine, machines-many  Machines with one node, or twelve rows (mocks/machines.ts)
 *   no-developer no run, no check and no Site API yet, so the developer places stay out of the menu
 *   env-gone   the idle environment's machine stopped for good (state gone, only Close is left)
 */
const KEY = 'agentos.mock.scenario';

function read(): Set<string> {
  if (typeof window === 'undefined') return new Set(['still']);
  const param = new URLSearchParams(window.location.search).get('mock');
  if (param !== null) sessionStorage.setItem(KEY, param);
  return new Set((param ?? sessionStorage.getItem(KEY) ?? '').split(',').filter(Boolean));
}

export const scenario = read();

const SESSION_KEY = 'agentos.mock.signed-out';

export const mockAuth = {
  get signedIn() {
    if (typeof window === 'undefined') return true;
    return localStorage.getItem(SESSION_KEY) !== '1';
  },
  set signedIn(value: boolean) {
    if (typeof window === 'undefined') return;
    if (value) localStorage.removeItem(SESSION_KEY);
    else localStorage.setItem(SESSION_KEY, '1');
  },
};
