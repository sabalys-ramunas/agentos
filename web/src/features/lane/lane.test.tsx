import { screen, within } from '@testing-library/react';
import type { Lane } from '@/api/types';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { holdNotice, proxyHost } from './lane';

const lane = (id: string) => db.orgs.get('northwind-group')!.lanes.find((item) => item.id === id)!;

/** As the live stack has Solstice's Instagram: switched off, and the site blocking it. */
function quarantineBlocked(target: Lane) {
  target.state = 'quarantined';
  target.signal = 'blocked';
  target.quarantine_reason = 'self-check failed: exit address did not match the pinned proxy';
  target.hold_until = null;
}

describe('the lane banner', () => {
  it('never asks to take over an account that is switched off, and points at Turn it back on instead', async () => {
    quarantineBlocked(lane('aster/instagram'));
    renderApp('/northwind-group/lanes/aster/instagram');
    const banner = (await screen.findByText('The site is refusing this account for now.')).closest('[role="status"]') as HTMLElement;
    expect(banner).toHaveTextContent('It is switched off for safety. Turn it back on below first, then take over to see what the site is showing.');
    // The action the banner names is on the page.
    expect(await screen.findByRole('button', { name: 'Turn it back on' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Take over' })).not.toBeInTheDocument();
  });

  it('tells someone who cannot operate the lane who can', () => {
    const target = { ...lane('aster/instagram'), state: 'warm' as const, signal: 'captcha' as const, hold_until: null, account: null };
    expect(holdNotice(target, true)?.body).toMatch(/^Take over/);
    expect(holdNotice(target, false)?.body).toBe('Only the owner of this computer can take over and sort it out.');
  });

  it('asks to open a sleeping lane before taking it over', () => {
    const target = { ...lane('aster/instagram'), state: 'cold' as const, signal: 'logged_out' as const, hold_until: null, account: null };
    expect(holdNotice(target)?.body).toMatch(/^Wake it up first\. Take over/);
  });
});

describe('Inspect', () => {
  it('shows a proxy the way the cloud sends it, by its label and never as undefined:undefined', async () => {
    lane('aster/instagram').proxy = { label: 'exit-aster' };
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    await user.click(await screen.findByRole('button', { name: 'Technical details' }));
    const drawer = within(await screen.findByRole('dialog'));
    expect(drawer.getByText('exit-aster')).toBeInTheDocument();
    expect(drawer.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(drawer.queryByText('Proxy host')).not.toBeInTheDocument();
  });

  it('shows the address only when both halves are there', () => {
    expect(proxyHost({ label: 'x', host: 'proxy.example', port: 8080 })).toBe('proxy.example:8080');
    expect(proxyHost({ label: 'x' })).toBeNull();
    expect(proxyHost(null)).toBeNull();
  });
});
