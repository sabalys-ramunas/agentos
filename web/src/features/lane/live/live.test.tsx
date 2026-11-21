import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import type { LaneControl } from '@/api/types';
import { idleControl } from '@/lib/lanes';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { holdsLane } from '../control';

const org = () => db.orgs.get('northwind-group')!;
const lane = (id: string) => org().lanes.find((item) => item.id === id)!;
const bar = (name: string) => screen.findByRole('region', { name: `Control of ${name}` });

describe('the lane window', () => {
  it('watches read-only until this person takes over, then hands keys to the lane', async () => {
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const view = await screen.findByTestId('vnc-screen');
    expect(view).toHaveAttribute('data-interactive', 'false');
    await user.click(within(await bar('Instagram')).getByRole('button', { name: 'Take over' }));
    await waitFor(() => expect(screen.getByTestId('vnc-screen')).toHaveAttribute('data-interactive', 'true'));
    await user.click(within(await bar('Instagram')).getByRole('button', { name: 'Hand back' }));
    await waitFor(() => expect(screen.getByTestId('vnc-screen')).toHaveAttribute('data-interactive', 'false'));
  });

  it('stays view only, and names the holder, when someone else has control', async () => {
    renderApp('/northwind-group/lanes/meridian/linkedin');
    const view = await screen.findByTestId('vnc-screen');
    expect(view).toHaveAttribute('data-interactive', 'false');
    expect(view).toHaveAttribute('data-holder', 'Mara Lindqvist');
  });

  it.each(['viewer', 'approver'] as const)('shows a %s the lane but never its screen (security review 30)', async (role) => {
    org().role = role;
    renderApp('/northwind-group/lanes/aster/instagram');
    expect(await screen.findByText('Live view is for members')).toBeInTheDocument();
    expect(screen.queryByTestId('vnc-screen')).not.toBeInTheDocument();
  });

  it('opens a terminal lane in the terminal, and tells a viewer who can open it', async () => {
    Object.assign(lane('personal/shell'), { state: 'warm' });
    renderApp('/northwind-group/lanes/personal/shell');
    expect(await screen.findByTestId('terminal-screen')).toBeInTheDocument();
    expect(screen.queryByText(/openajar status/)).not.toBeInTheDocument();
  });

  it('shows a viewer of a terminal lane what it is, without connecting', async () => {
    org().role = 'viewer';
    Object.assign(lane('personal/shell'), { state: 'warm' });
    renderApp('/northwind-group/lanes/personal/shell');
    expect(await screen.findByText('Only the owner of this computer can open it here.')).toBeInTheDocument();
    expect(screen.queryByTestId('terminal-screen')).not.toBeInTheDocument();
  });

  it('says a service lane has no screen, instead of drawing a prompt', async () => {
    Object.assign(lane('personal/notes-sync'), { state: 'warm' });
    renderApp('/northwind-group/lanes/personal/notes-sync');
    expect(await screen.findByText('This tool runs a service')).toBeInTheDocument();
  });

  it('fills the window and leaves it with Escape, keeping focus on the toggle', async () => {
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    await screen.findByTestId('vnc-screen');
    await user.click(screen.getByRole('button', { name: 'Full window' }));
    expect(await bar('Instagram')).toHaveClass('fixed');
    expect(document.body.style.overflow).toBe('hidden');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.getByRole('region', { name: 'Control of Instagram' })).not.toHaveClass('fixed'));
    expect(document.body.style.overflow).toBe('');
  });
});

describe('keys typed into a lane', () => {
  function laneInput() {
    const view = screen.getByTestId('vnc-screen');
    const target = document.createElement('div');
    target.setAttribute('data-lane-input', '');
    target.tabIndex = 0;
    view.append(target);
    target.focus();
    return target;
  }

  it('never fire the panel shortcuts: not T, not g then n, not Cmd-K', async () => {
    const { router } = renderApp('/northwind-group/lanes/aster/instagram');
    await screen.findByTestId('vnc-screen');
    const target = laneInput();
    fireEvent.keyDown(target, { key: 't' });
    fireEvent.keyDown(target, { key: 'g' });
    fireEvent.keyDown(target, { key: 'n' });
    fireEvent.keyDown(target, { key: 'k', metaKey: true });
    fireEvent.keyDown(target, { key: 'k', ctrlKey: true });
    fireEvent.keyDown(target, { key: '?' });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(lane('aster/instagram').control.state).toBe('agent');
    expect(router.state.location.pathname).toBe('/northwind-group/lanes/aster/instagram');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('still fire outside the lane', async () => {
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    await screen.findByTestId('vnc-screen');
    await user.keyboard('t');
    await waitFor(() => expect(lane('aster/instagram').control.state).toBe('human'));
  });
});

describe('who holds the lane', () => {
  const me = { id: 'usr_DEMO', name: 'Mara Lindqvist', email: 'mara@example.com' };
  const held = (by: string, extra: Partial<LaneControl> = {}): LaneControl => ({
    ...idleControl,
    state: 'human',
    by,
    since: new Date().toISOString(),
    ...extra,
  });

  it('is decided by user id, so taking someone else\'s name gives nothing', () => {
    expect(holdsLane(held('Mara Lindqvist', { by_id: 'usr_MARA' }), me)).toBe(false);
    expect(holdsLane(held('Mara Lindqvist', { by_id: 'usr_DEMO' }), me)).toBe(true);
    expect(holdsLane(held('Mara Lindqvist', { by_id: null }), me)).toBe(false);
  });

  it('counts only by_id: an id or a name in by never counts, and never an agent', () => {
    expect(holdsLane(held('usr_DEMO'), me)).toBe(false);
    expect(holdsLane(held('Mara Lindqvist'), me)).toBe(false);
    expect(holdsLane({ ...idleControl, by: 'usr_DEMO' }, me)).toBe(false);
    expect(holdsLane(held('usr_DEMO'), undefined)).toBe(false);
  });

  it('keeps a renamed viewer of a held lane out of the keys', async () => {
    db.user.name = 'Mara Lindqvist';
    renderApp('/northwind-group/lanes/meridian/linkedin');
    const view = await screen.findByTestId('vnc-screen');
    expect(view).toHaveAttribute('data-interactive', 'false');
    expect(view).toHaveAttribute('data-holder', 'Mara Lindqvist');
  });
});
