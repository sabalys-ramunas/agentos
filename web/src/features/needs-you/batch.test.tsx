import { screen, waitFor, within } from '@testing-library/react';
import { toast } from 'sonner';
import type * as Sonner from 'sonner';
import type { Approval } from '@/api/types';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { rowTitleOf, whereOf } from './words';

vi.mock('sonner', async (importOriginal) => {
  const actual = await importOriginal<typeof Sonner>();
  return { ...actual, toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }) };
});

const PATH = '/northwind-group/needs-you';
const org = () => db.orgs.get('northwind-group')!;
const toastMock = toast as unknown as ReturnType<typeof vi.fn> & { error: ReturnType<typeof vi.fn> };

/** Picks the posts group's items, then lets the server move on before the decision lands. */
async function start() {
  const view = renderApp(PATH);
  const section = await screen.findByRole('region', { name: 'Posts to check' });
  await screen.findByRole('button', { name: /^Instagram post, Aster Coffee/ });
  const commits = org().approvals.filter((item) => item.effect === 'commit');
  return { ...view, section, commits };
}

/** What a row is called on screen: "Instagram post, Aster Coffee". */
const rowLabel = (item: Approval) => {
  const business = org().workspaces.find((workspace) => workspace.id === item.lane.split('/')[0])?.name ?? '';
  return `${rowTitleOf(item, whereOf(item, business))}, ${business}`;
};
const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const checkbox = (item: Approval) => screen.getByRole('checkbox', { name: new RegExp(`^Choose: ${escape(rowLabel(item))}`) });

async function pick(user: ReturnType<typeof renderApp>['user'], items: Approval[]) {
  if (!screen.queryByRole('button', { name: 'Done choosing' })) await user.click(screen.getByRole('button', { name: 'Choose several' }));
  for (const item of items) await user.click(checkbox(item));
  await screen.findByText(`${items.length} posts chosen`);
}

const barButton = (name: string) => within(screen.getByText(/\d+ posts? chosen/).closest('div')!).getByRole('button', { name });
const changeOnTheServer = (id: string) => {
  org().approvals.find((item) => item.id === id)!.content_hash = 'sha256:changed-after-it-was-read';
};
const decideElsewhere = (id: string) => {
  org().approvals = org().approvals.filter((item) => item.id !== id);
};

beforeEach(() => {
  toastMock.mockClear();
  toastMock.error.mockClear();
});

describe('a batch decision', () => {
  it('counts what went through and what did not, and keeps refused items in view with the reason', async () => {
    const { user, commits } = await start();
    const [fine, changed, taken] = commits as [Approval, Approval, Approval];
    await pick(user, [fine, changed, taken]);
    changeOnTheServer(changed.id);
    decideElsewhere(taken.id);
    await user.click(barButton('Post 3'));

    const report = await screen.findByRole('alert');
    expect(within(report).getByText('You said yes to 1. 2 did not go through.')).toBeInTheDocument();
    expect(within(report).getByText(changed.summary)).toBeInTheDocument();
    expect(within(report).getByText('This changed, so nothing was decided. Review it again.')).toBeInTheDocument();
    expect(within(report).getByText(taken.summary)).toBeInTheDocument();
    expect(within(report).getByText('Someone already decided this.')).toBeInTheDocument();
    expect(org().approvals.some((item) => item.id === fine.id)).toBe(false);
  }, 15_000);

  it('never rolls a partial result into a success toast', async () => {
    const { user, commits } = await start();
    const [fine, changed] = commits as [Approval, Approval];
    await pick(user, [fine, changed]);
    changeOnTheServer(changed.id);
    await user.click(barButton('Post 2'));
    await screen.findByRole('alert');
    await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('You said yes to 1. 1 did not go through.'));
    expect(toastMock).not.toHaveBeenCalled();
  }, 15_000);

  it('brings a changed item back to the queue, marked, unselected and focused', async () => {
    const { user, commits } = await start();
    const [fine, changed] = commits as [Approval, Approval];
    await pick(user, [fine, changed]);
    changeOnTheServer(changed.id);
    await user.click(barButton('Post 2'));
    await screen.findByRole('alert');

    const row = await screen.findByRole('button', { name: new RegExp(`^${escape(rowLabel(changed))}.*changed, read it again`) });
    expect(within(row).getByText('Changed, read it again')).toBeInTheDocument();
    await waitFor(() => expect(row).toHaveAttribute('aria-current', 'true'));
    expect(screen.queryByText(/\d+ posts? chosen/)).toBeNull();
    // A changed post is never chosen again for a batch by itself.
    expect(screen.queryByRole('checkbox', { name: new RegExp(`^Choose: ${escape(rowLabel(changed))}`) })).toBeNull();
  }, 15_000);

  it('brings a refused item back while the pointer is still over the list', async () => {
    const { user, commits } = await start();
    const [fine, changed] = commits as [Approval, Approval];
    await pick(user, [fine, changed]);
    changeOnTheServer(changed.id);
    // The batch bar sits inside the list, so the pointer is holding it still at this moment.
    await user.hover(checkbox(fine));
    await user.click(barButton('Post 2'));
    await screen.findByRole('alert');
    expect(await screen.findByRole('button', { name: new RegExp(`^${escape(rowLabel(changed))}`) })).toBeInTheDocument();
  });

  it('gives the waiting count back for what is still waiting', async () => {
    const { user, commits, client } = await start();
    const [fine, changed] = commits as [Approval, Approval];
    await pick(user, [fine, changed]);
    changeOnTheServer(changed.id);
    await user.click(barButton('Post 2'));
    await screen.findByRole('alert');
    const pending = () => org().approvals.length;
    await waitFor(() => expect(client.getQueryData<{ id: string }[]>(['org', 'northwind-group', 'approvals', 'pending'])).toHaveLength(pending()));
  });

  it('says none went through when none did', async () => {
    const { user, commits } = await start();
    const [one, two] = commits as [Approval, Approval];
    await pick(user, [one, two]);
    decideElsewhere(one.id);
    decideElsewhere(two.id);
    await user.click(barButton("Don't post"));
    expect(await screen.findByText('None of the 2 items went through.')).toBeInTheDocument();
    expect(toastMock).not.toHaveBeenCalled();
  });

  it('uses the right word for a deny', async () => {
    const { user, commits } = await start();
    const [fine, changed] = commits as [Approval, Approval];
    await pick(user, [fine, changed]);
    changeOnTheServer(changed.id);
    await user.click(barButton("Don't post"));
    expect(await screen.findByText('You said no to 1. 1 did not go through.')).toBeInTheDocument();
  });

  it('keeps the report when the queue empties, instead of saying all caught up', async () => {
    const data = org();
    const [one, two] = data.approvals as [Approval, Approval];
    data.approvals = [one, two];
    data.questions = [];
    for (const lane of data.lanes) {
      lane.signal = 'ok';
      lane.hold = null;
      lane.control = { ...lane.control, state: 'agent', takeback_requested: false } as typeof lane.control;
      lane.quarantine_reason = null;
    }
    const { user } = renderApp(PATH);
    await screen.findByRole('button', { name: new RegExp(`^${escape(rowLabel(one))}`) });
    await pick(user, [one, two]);
    decideElsewhere(one.id);
    decideElsewhere(two.id);
    await user.click(barButton('Post 2'));
    expect(await screen.findByText('None of the 2 items went through.')).toBeInTheDocument();
    expect(await screen.findByText(one.summary)).toBeInTheDocument();
  });

  it('survives leaving the page, and goes only when dismissed', async () => {
    const { user, commits, router } = await start();
    const [fine, taken] = commits as [Approval, Approval];
    await pick(user, [fine, taken]);
    decideElsewhere(taken.id);
    await user.click(barButton('Post 2'));
    await screen.findByRole('alert');

    await router.navigate({ to: '/$org/flows', params: { org: 'northwind-group' } });
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    await router.navigate({ to: '/$org/needs-you', params: { org: 'northwind-group' } });
    const report = await screen.findByRole('alert');
    expect(within(report).getByText('You said yes to 1. 1 did not go through.')).toBeInTheDocument();

    await user.click(within(report).getByRole('button', { name: 'Dismiss this report' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });

  it('reports an item the reply left out as not decided', async () => {
    const { fleet } = await import('@/api/client');
    const spy = vi.spyOn(fleet, 'decideBatch').mockImplementationOnce(async (_org, items) => {
      const [first] = items;
      return [{ ...org().approvals.find((item) => item.id === first?.id)!, ok: true as const }];
    });
    const { user, commits } = await start();
    const [fine, missing] = commits as [Approval, Approval];
    await pick(user, [fine, missing]);
    await user.click(barButton('Post 2'));
    expect(await screen.findByText(/No answer came back/)).toBeInTheDocument();
    spy.mockRestore();
  });

  it('still says plainly when everything went through', async () => {
    const { user, commits } = await start();
    const [one, two] = commits as [Approval, Approval];
    await pick(user, [one, two]);
    await user.click(barButton('Post 2'));
    // Never "Approved": it says what is under way, with Undo.
    expect(await screen.findByText(/^Posting 2 posts in \d s$/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument();
    expect(toastMock).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
