import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach } from 'vitest';
import { db } from '@/mocks/handlers';
import { writeTechnical } from '@/lib/technical';
import { renderApp } from '@/test/render';
import { backupAnswer } from './tab-backups';
import { environmentsAnswer } from './tab-environments';
import { healthAnswer } from './tab-health';
import { identityAnswer } from './tab-identity';
import type { HealthCheck, IdentityAccount, NodeBackups, NodeEnvironments } from '@/api/types';

const PATH = '/northwind-group/nodes/hetzner-1';
const AT = '2026-11-06T12:00:00.000Z';

const check = (status: HealthCheck['status'], name = status): HealthCheck => ({ name, status, detail: '', at: AT });

describe('the sentence a person reads first', () => {
  it('says what fails before what needs a look', () => {
    expect(healthAnswer([check('ok'), check('ok')])).toBe('All 2 checks pass.');
    expect(healthAnswer([check('ok'), check('warn')])).toBe('1 check needs a look. 1 of 2 pass.');
    expect(healthAnswer([check('fail'), check('warn'), check('ok')])).toBe('1 check fails, and 1 needs a look. Fix them first.');
    expect(healthAnswer([check('fail'), check('fail'), check('ok')])).toBe('2 checks fail. Fix them first.');
  });

  it('says when a backup ran, and asks for one only when nothing is backed up', () => {
    const empty: NodeBackups = { folder: '/x', keep: 7, items: [], job: null };
    expect(backupAnswer(empty)).toMatch(/^No backup yet\./);
    const now = Date.parse(AT);
    const item = (state: 'verified' | 'failed' | 'not_checked', hoursAgo = 3) => ({
      name: 'agentos-dev-20261106T090000Z.tar.gz.age',
      created_at: new Date(now - hoursAgo * 3_600_000).toISOString(),
      size: 1024,
      check: { state, at: null, error: null },
    });
    const ok: NodeBackups = { folder: '/x', keep: 7, items: [item('verified'), item('verified', 30)], job: null };
    expect(backupAnswer(ok, now)).toBe('The last backup ran 3h ago and checked out. 2 of 7 are kept.');
    expect(backupAnswer({ ...ok, items: [item('failed')] }, now)).toMatch(/did not check out/);
    expect(backupAnswer({ ...ok, items: [item('not_checked')] }, now)).toBe('The last backup ran 3h ago. It has not been checked yet.');
    expect(backupAnswer({ ...ok, items: [item('verified', 24 * 9)] }, now)).toMatch(/Make a new one/);
  });

  it('says how many services run, or that none are set up', () => {
    const base: NodeEnvironments = { lanes: [], services: [], images: { templates: [], built: [] } };
    expect(environmentsAnswer(base)).toMatch(/runs no services yet/);
    const row = (state: string) => ({ lane: 'a/b', name: 'b', state, desired: 'running', health: 'none', restarts: 0, last_exit: null, started_at: null });
    expect(environmentsAnswer({ ...base, services: [row('running')] })).toBe('The one service is running.');
    expect(environmentsAnswer({ ...base, services: [row('running'), row('stopped')] })).toBe('1 of 2 services running.');
  });

  it('says which accounts sign, and to how many sites', () => {
    const account = (state: IdentityAccount['state'], sites = 0): IdentityAccount => ({
      lane: 'a/b',
      state,
      keyid: null,
      signature_agent: null,
      hosts: Array.from({ length: sites }, (_, index) => ({ origin: `https://s${index}.example`, reason: 'r', added_by: null, added_at: null })),
    });
    expect(identityAnswer([])).toBe('No account on this machine yet.');
    expect(identityAnswer([account('none')])).toMatch(/No account signs/);
    expect(identityAnswer([account('signing', 2), account('off')])).toBe('1 account signs its requests, to 2 sites.');
  });
});

describe('the computer ops screens', () => {
  it('runs the full check on request, with a running state, and keeps its answer', async () => {
    const { user } = renderApp(`${PATH}?tab=health`);
    const panel = await screen.findByRole('region', { name: 'Health' });
    // The button is offered once the node has answered whether it has rounds, so it is awaited.
    await user.click(await within(panel).findByRole('button', { name: 'Run the full check' }));
    expect(await within(panel).findByText(/Checking now\./)).toBeInTheDocument();
    expect(await within(panel).findByText(/Full check ran/, undefined, { timeout: 4000 })).toBeInTheDocument();
  });

  it('shows the fix for a check that needs one in plain words, and the upgrade command only with Technical details', async () => {
    renderApp(`${PATH}?tab=health`);
    expect(await screen.findByText(/Free some disk/)).toBeInTheDocument();
    const version = await screen.findByRole('region', { name: 'Version' });
    expect(await within(version).findByText(/cannot see a newer release/)).toBeInTheDocument();
    expect(version.querySelector('code')).toBeNull();
    expect(within(version).getByText(/Upgrading restarts agentos on this machine/)).toBeInTheDocument();
  });

  it('shows the command a check needs under Technical details, and the sentence without it', async () => {
    writeTechnical(db.user.id, true);
    renderApp(`${PATH}?tab=health`);
    const version = await screen.findByRole('region', { name: 'Version' });
    expect(await within(version).findByText(/Upgrading restarts agentos on this machine/)).toBeInTheDocument();
    // The command is split into words for display, so it is read as the code element's text.
    expect(version.querySelector('code')?.textContent).toBe('agentos upgrade --source <your agentos checkout>');
  }, 15_000);

  beforeEach(() => writeTechnical(db.user.id, false));
  afterEach(() => writeTechnical(db.user.id, false));

  it('makes a backup from the backups tab and says what it is doing', async () => {
    const { user } = renderApp(`${PATH}?tab=backups`);
    const section = await screen.findByRole('region', { name: 'Backups' });
    expect(within(section).getByText(/The last backup ran .* and checked out\. 2 of 7 are kept\./)).toBeInTheDocument();
    await user.click(within(section).getByRole('button', { name: 'Make a backup' }));
    expect(await screen.findByText('Making the backup.')).toBeInTheDocument();
  });

  it('keeps the restore on the command line, behind Technical details, with the reason', async () => {
    renderApp(`${PATH}?tab=backups`);
    const restore = await screen.findByRole('region', { name: 'Restore' });
    expect(within(restore).getByText(/Restoring writes over everything on this machine/)).toBeInTheDocument();
    expect(restore.querySelector('code')?.textContent).toMatch(/^agentos backup restore .*--passphrase-file/);
  });

  it('starts and stops a service, and reads its log when asked', async () => {
    const { user } = renderApp(`${PATH}?tab=environments`);
    const services = await screen.findByRole('region', { name: 'Services' });
    expect(within(services).getByText('portal-api')).toBeInTheDocument();
    await user.click(within(services).getAllByRole('button', { name: 'Logs' })[0]!);
    expect(await screen.findByText(/listening on :8080/)).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await user.click(within(services).getByRole('button', { name: 'Start' }));
    expect(await within(services).findByText('Stop')).toBeInTheDocument();
  });

  it('adds a site to an account that signs, and the site shows at once', async () => {
    const { user } = renderApp(`${PATH}?tab=connection`);
    const identity = await screen.findByRole('region', { name: 'Identity on sites' });
    expect(within(identity).getByText('1 account signs its requests, to 1 site.')).toBeInTheDocument();
    await user.type(within(identity).getByLabelText('Site'), 'example.org');
    await user.type(within(identity).getByLabelText('Why'), 'Read-only research');
    await user.click(within(identity).getByRole('button', { name: 'Add site' }));
    expect(await within(identity).findByText('https://example.org')).toBeInTheDocument();
  });

  it('reads the settings, with a secret shown only as set', async () => {
    renderApp(`${PATH}?tab=settings`);
    const settings = await screen.findByRole('region', { name: 'Settings' });
    expect(within(settings).getByText('(set, hidden)')).toBeInTheDocument();
    expect(within(settings).getByText(/Read-only\./)).toBeInTheDocument();
  });
});
