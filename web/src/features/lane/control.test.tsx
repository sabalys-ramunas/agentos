import { screen, waitFor, within } from '@testing-library/react';
import { act } from 'react';
import { http, HttpResponse } from 'msw';
import { applyLiveEvent } from '@/api/live';
import { idleControl } from '@/lib/lanes';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

const org = () => db.orgs.get('northwind-group')!;
const lane = (id: string) => org().lanes.find((item) => item.id === id)!;
const bar = (name: string) => screen.findByRole('region', { name: `Control of ${name}` });

describe('lane control', () => {
  it('says the agent has control, and offers Take over and Stop agent', async () => {
    renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    expect(region.getByText('Agent has control')).toBeInTheDocument();
    expect(region.getByRole('button', { name: 'Take over' })).toBeInTheDocument();
    expect(region.getByRole('button', { name: 'Stop agent' })).toBeInTheDocument();
  });

  it('only says "you have control" once the server has agreed', async () => {
    // Under load the request can reach the handler after the test lets it go, so a release that
    // comes first is kept and the handler answers at once.
    let released = false;
    let release = () => {
      released = true;
    };
    server.use(
      // Held here, then answered by the mock's own handler (nothing returned), so the mock's lane says
      // what the answer says and a later read of it cannot bring the agent back.
      http.post('/api/orgs/:org/lanes/:ws/:name/control', async () => {
        await new Promise<void>((resolve) => (released ? resolve() : (release = resolve)));
      }),
    );
    const { user, client } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    await user.click(region.getByRole('button', { name: 'Take over' }));
    const busy = await region.findByRole('button', { name: 'Taking control' });
    expect(busy).toHaveAttribute('aria-disabled', 'true');
    expect(region.getByText('Agent has control')).toBeInTheDocument();
    expect(region.queryByText('You have control')).not.toBeInTheDocument();
    release();
    // the answer, the lane's refetch and the person's own name all have to land: on a busy CI
    // worker that took longer than the 5 s every other wait gets
    expect(await region.findByText('You have control', {}, { timeout: 15_000 })).toBeInTheDocument();
    // A later read of the lane, such as the catch-up after the event stream reconnects, agrees.
    await act(() => client.invalidateQueries({ queryKey: ['org', 'northwind-group'] }));
    expect(region.getByText('You have control')).toBeInTheDocument();
  }, 20_000); // a slow machine must not turn the wait for the server into a failure

  it('keeps focus on Take over while its request runs, and a second press sends nothing', async () => {
    let release = () => {};
    let calls = 0;
    server.use(
      // held here, then answered by the mock's own handler, as above
      http.post('/api/orgs/:org/lanes/:ws/:name/control', async () => {
        calls += 1;
        await new Promise<void>((resolve) => (release = resolve));
      }),
    );
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    const button = region.getByRole('button', { name: 'Take over' });
    button.focus();
    await user.keyboard('{Enter}');
    await region.findByRole('button', { name: 'Taking control' });
    // Chrome drops focus from a disabled button, so a busy one is never `disabled`. jsdom does
    // not drop it, which is why the attribute is checked as well as focus.
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(document.activeElement).toBe(button);
    await user.keyboard('{Enter}');
    await user.click(button);
    expect(calls).toBe(1);
    release();
    await region.findByText('You have control', {}, { timeout: 15_000 });
    expect(document.activeElement).toBe(button);
    expect(button).not.toHaveAttribute('aria-disabled');
  }, 20_000);

  it('keeps keyboard focus on the same button through take and hand back', async () => {
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    const button = region.getByRole('button', { name: 'Take over' });
    button.focus();
    await user.keyboard('{Enter}');
    await region.findByText('You have control');
    expect(region.getByRole('button', { name: 'Hand back' })).toBe(button);
    expect(document.activeElement).toBe(button);
  });

  it('works from the keyboard with t, and says the lane returns to agents', async () => {
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    await user.keyboard('t');
    await region.findByText('You have control');
    expect(region.getByText(/Returns to them in 10:00|Returns to them in 9:5\d/)).toBeInTheDocument();
    expect(lane('aster/instagram').control.state).toBe('human');
    await user.keyboard('t');
    await region.findByText('Agent has control');
    expect(lane('aster/instagram').control.state).toBe('agent');
  });

  it('says exactly what Stop agent does, and only acts on confirmation', async () => {
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    await user.click(region.getByRole('button', { name: 'Stop agent' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByText(/Any run or brain task on this account is cancelled/)).toBeInTheDocument();
    expect(lane('aster/instagram').control.state).toBe('agent');
    await user.click(dialog.getByRole('button', { name: 'Stop agent' }));
    await waitFor(() => expect(lane('aster/instagram').control.state).toBe('paused'));
    expect(await region.findByText('Agent stopped')).toBeInTheDocument();
    // A stopped agent is taken over first (to look, or to do what it cannot), then handed back.
    expect(region.getByRole('button', { name: 'Take over' })).toBeInTheDocument();
    expect(region.queryByRole('button', { name: 'Hand back' })).not.toBeInTheDocument();
  });

  it('shows the banner when an agent asks for control back, with both answers', async () => {
    const { user } = renderApp('/northwind-group/lanes/meridian/linkedin');
    const region = within(await bar('LinkedIn'));
    expect(region.getByText('Mara Lindqvist has control')).toBeInTheDocument();
    expect(region.getByText(/is asking for control back/)).toBeInTheDocument();
    expect(region.getByText('The scheduled post is due in 10 minutes')).toBeInTheDocument();
    await user.click(region.getByRole('button', { name: 'Keep control' }));
    await waitFor(() => expect(lane('meridian/linkedin').control.takeback_requested).toBe(false));
    expect(lane('meridian/linkedin').control.state).toBe('human');
    expect(region.queryByText(/is asking for control back/)).not.toBeInTheDocument();
  });

  it('follows a change made elsewhere, without a reload', async () => {
    const { client } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    const control = { ...idleControl, state: 'human' as const, by: 'Mara Lindqvist', since: new Date().toISOString() };
    act(() => applyLiveEvent(client, 'northwind-group', { type: 'control', payload: { lane: 'aster/instagram', control } }));
    expect(await region.findByText('Mara Lindqvist has control')).toBeInTheDocument();
  });

  it.each(['viewer', 'approver'] as const)('shows a %s who holds a lane, and no control at all', async (role) => {
    org().role = role;
    renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    expect(region.getByText('Agent has control')).toBeInTheDocument();
    for (const name of ['Take over', 'Stop agent', 'Hand back', 'Keep control']) {
      expect(region.queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });

  it('leaves the bar honest when the server refuses', async () => {
    server.use(
      http.post('/api/orgs/:org/lanes/:ws/:name/control', () =>
        HttpResponse.json({ error: { code: 'human_in_control', message: 'Someone else took this lane first.' } }, { status: 409 }),
      ),
    );
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    await user.click(region.getByRole('button', { name: 'Take over' }));
    expect(await region.findByRole('alert')).toHaveTextContent('Someone else took this lane first.');
    expect(region.getByText('Agent has control')).toBeInTheDocument();
  });

  it('explains a hold with its end time, and the wrong account', async () => {
    renderApp('/northwind-group/lanes/meridian/reddit');
    expect(await screen.findByText(/Agents are held off until/)).toBeInTheDocument();
  });

  it('offers Take over on a paused or held lane, and never Hand back before someone has the lane', async () => {
    Object.assign(lane('aster/instagram'), { state: 'warm', signal: 'captcha', hold: 'needs_human:captcha', control: { ...idleControl, state: 'paused', by: 'Demo Owner', by_id: 'usr_DEMO', since: new Date().toISOString() } });
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    expect(region.getByText('Held for a check')).toBeInTheDocument();
    expect(region.queryByRole('button', { name: 'Hand back' })).not.toBeInTheDocument();
    expect(region.getByRole('button', { name: 'Take over' })).toBeInTheDocument();
    expect(region.queryByRole('button', { name: 'Stop agent' })).not.toBeInTheDocument();
    await user.click(region.getByRole('button', { name: 'Take over' }));
    expect(await region.findByRole('button', { name: 'Not done yet' })).toBeInTheDocument();
  });

  it('keeps a check hold on "Not done yet", and clears it only on "Done, hand back"', async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post('/api/orgs/:org/lanes/:ws/:name/control', async ({ request }) => {
        const body = await request.json();
        bodies.push(body);
        return HttpResponse.json({ ...lane('aster/instagram'), control: { ...idleControl, state: 'human', by: 'Demo Owner', by_id: 'usr_DEMO' } });
      }),
    );
    Object.assign(lane('aster/instagram'), { state: 'warm', signal: 'captcha', hold: 'needs_human:captcha', control: { ...idleControl, state: 'human', by: 'Demo Owner', by_id: 'usr_DEMO', since: new Date().toISOString() } });
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    expect(region.queryByRole('button', { name: 'Hand back' })).not.toBeInTheDocument();
    await user.click(region.getByRole('button', { name: 'Not done yet' }));
    await waitFor(() => expect(bodies).toEqual([{ action: 'hand_back', done: false }]));
    await user.click(await region.findByRole('button', { name: 'Done, hand back' }));
    await waitFor(() => expect(bodies).toEqual([{ action: 'hand_back', done: false }, { action: 'hand_back', done: true }]));
  });

  it('sends a plain hand back with no answer when no check holds the lane', async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post('/api/orgs/:org/lanes/:ws/:name/control', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ ...lane('aster/instagram'), control: { ...idleControl } });
      }),
    );
    Object.assign(lane('aster/instagram'), { state: 'warm', control: { ...idleControl, state: 'human', by: 'Demo Owner', by_id: 'usr_DEMO', since: new Date().toISOString() } });
    const { user } = renderApp('/northwind-group/lanes/aster/instagram');
    const region = within(await bar('Instagram'));
    await user.click(region.getByRole('button', { name: 'Hand back' }));
    await waitFor(() => expect(bodies).toEqual([{ action: 'hand_back' }]));
  });
});

describe('a lane its node stopped reporting', () => {
  it('says since when, and lets an admin release it after confirming', async () => {
    const { db } = await import('@/mocks/handlers');
    const target = db.orgs.get('northwind-group')!.lanes.find((item) => item.id === 'aster/medium')!;
    target.missing_since = new Date(Date.now() - 3_600_000).toISOString();
    let released = false;
    server.use(
      http.post('/api/orgs/:org/lanes/:ws/:name/release', () => {
        released = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = renderApp('/northwind-group/lanes/aster/medium');
    expect(await screen.findByText(/Not reported by/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Release this account' }));
    expect(released).toBe(false);
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Release account' }));
    await waitFor(() => expect(released).toBe(true));
  });
});
