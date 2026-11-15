import { screen, waitFor, within } from '@testing-library/react';
import { renderApp } from '@/test/render';
import { canonicalUrl, toInternalPath, toPublicPath } from './paths';

describe('everyday names in the address bar', () => {
  it.each([
    ['/acme/history', '/acme/activity'],
    ['/acme/routines', '/acme/flows'],
    ['/acme/routines/aster.instagram.post', '/acme/flows/aster.instagram.post'],
    ['/acme/machines', '/acme/nodes'],
    ['/acme/machines/home-mini', '/acme/nodes/home-mini'],
    ['/acme/accounts/aster/instagram', '/acme/lanes/aster/instagram'],
  ])('reads %s as %s inside', (outside, inside) => {
    expect(toInternalPath(outside)).toBe(inside);
    expect(toPublicPath(inside)).toBe(outside);
  });

  it('leaves the Accounts page, other pages and pages that are not an org alone', () => {
    for (const path of ['/acme/accounts', '/acme', '/acme/needs-you', '/acme/settings/general', '/onboarding/acme/nodes', '/sign-in', '/']) {
      expect(toInternalPath(path)).toBe(path);
      expect(toPublicPath(path)).toBe(path);
    }
  });

  it('still reads Machines by the name it had before Mono', () => {
    expect(toInternalPath('/acme/computers/home-mini')).toBe('/acme/nodes/home-mini');
    expect(canonicalUrl('/acme/computers/home-mini', '?tab=logs')).toBe('/acme/machines/home-mini?tab=logs');
    expect(canonicalUrl('/acme/nodes')).toBe('/acme/machines');
  });

  it('gives an old address its new name, and nothing for a new one', () => {
    expect(canonicalUrl('/acme/activity', '?run=r1')).toBe('/acme/history?run=r1');
    expect(canonicalUrl('/acme/lanes/aster/reddit')).toBe('/acme/accounts/aster/reddit');
    expect(canonicalUrl('/acme/history')).toBeNull();
    expect(canonicalUrl('/acme/machines/home-mini')).toBeNull();
    expect(canonicalUrl('/acme/runs/job-1')).toBeNull();
  });

  it.each(['/northwind-group/nodes/home-mini', '/northwind-group/computers/home-mini', '/northwind-group/machines/home-mini'])(
    'opens %s, an old link or the new one, on the same page',
    async (path) => {
      const { router } = renderApp(path);
      // The machine opens in a dialog over Machines, so the page's heading may already sit behind it.
      expect(await screen.findByRole('heading', { level: 1, hidden: true })).toBeInTheDocument();
      expect(router.state.location.pathname).toBe('/northwind-group/nodes/home-mini');
    },
  );

  it('opens the new address and links with the new names', async () => {
    const { router } = renderApp('/northwind-group/history');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/activity'));
    const rail = within((await screen.findAllByRole('navigation', { name: 'Main' }))[0]!);
    expect(rail.getByRole('link', { name: 'History' })).toHaveAttribute('href', '/northwind-group/history');
    expect(rail.getByRole('link', { name: 'Accounts' })).toHaveAttribute('href', '/northwind-group/accounts');
    expect(rail.getByRole('link', { name: 'Routines' })).toHaveAttribute('href', '/northwind-group/routines');
    expect(rail.getByRole('link', { name: /Machines/ })).toHaveAttribute('href', '/northwind-group/machines');
    expect(rail.getByRole('link', { name: 'Runs' })).toHaveAttribute('href', '/northwind-group/runs');
  });
});
