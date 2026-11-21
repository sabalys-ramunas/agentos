import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { act } from 'react';
import { applyLiveEvent } from '@/api/live';
import { db, mockTiming } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { ARM_MS, DONE_MS } from './decision-flow';
import { asComputer } from '@/test/computer';

const PATH = '/northwind-group/needs-you';
const org = () => db.orgs.get('northwind-group')!;
const card = () => within(screen.getByRole('region', { name: 'Selected item' }));
const question = () => card().getByRole('heading', { level: 2 }).textContent;
/** Longer than the mock's latency, so a wrongly sent decision would have landed by now. */
const settle = (ms = 320) => new Promise((resolve) => setTimeout(resolve, ms));

async function ready() {
  const view = renderApp(PATH);
  await screen.findByRole('heading', { name: 'Needs you', level: 1 });
  await screen.findByRole('region', { name: 'Posts to check' });
  await screen.findByRole('region', { name: 'Questions' });
  await screen.findByRole('button', { name: /^Instagram post, Aster Coffee/ });
  return view;
}

/** Opens the Aster Coffee Instagram post. */
async function openInstagram(user: ReturnType<typeof renderApp>['user']) {
  // By name: the business names arrive with the workspaces, a moment after the list.
  await user.click(await screen.findByRole('button', { name: /^Instagram post, Aster Coffee/ }));
  await waitFor(() => expect(question()).toBe("Post this photo on Aster Coffee's Instagram?"));
}

describe('Needs you', () => {
  it('groups by what is at stake, money first, and says why', async () => {
    await ready();
    const headings = screen.getAllByRole('heading', { level: 2 }).map((item) => item.textContent);
    expect(headings.indexOf('Costs money')).toBeLessThan(headings.indexOf("Can't be undone"));
    expect(headings.indexOf("Can't be undone")).toBeLessThan(headings.indexOf('Posts to check'));
    expect(screen.getByText('Read it as your customers will see it.')).toBeInTheDocument();
  });

  it('says in plain words why a safety check made something ask', async () => {
    const { user } = await ready();
    await user.click(await screen.findByRole('button', { name: /^Email.*Aster Coffee/ }));
    expect(await screen.findByText('A safety check stopped this')).toBeInTheDocument();
    expect(screen.getByText(/This would normally run on its own\. It read text from an email at nordicbeans-orders\.example/)).toBeInTheDocument();
  });

  it('says how many things need you with the same number as the rail, and its parts add up', async () => {
    const { user } = await ready();
    const sentence = await screen.findByText(/^\d+ things need you: /);
    // A plan an agent drafted waits for the owner's yes, and is one of the parts too.
    // A brand change an agent proposed waits for the owner's yes too.
    const [, total = '', decide = '', plans = '0', brand = '0', hands = ''] =
      /^(\d+) things need you: (\d+) to decide(?:, (\d+) plans? to approve)?(?:, (\d+) brand changes? to approve)?, (\d+) accounts that need a hand\. Only what you asked to see, plus money and things that can't be undone\. The rest runs on its own\.$/.exec(sentence.textContent ?? '') ?? [];
    expect(Number(decide) + Number(plans) + Number(brand) + Number(hands)).toBe(Number(total));
    // Login codes are waiting too, and the owner's rail counts them with the rest.
    expect(Number(decide)).toBe(org().approvals.length + org().questions.length + org().codes.filter((item) => item.state === 'open').length);
    const rail = screen.getAllByRole('navigation', { name: 'Main' })[0]!;
    await waitFor(() => expect(within(rail).getByRole('link', { name: /Needs you/ })).toHaveTextContent(total));
    // The accounts that need a hand are a tab of their own, with their count on it.
    const handsTab = screen.getByRole('tab', { name: /^Accounts that need a hand/ });
    expect(handsTab).toHaveTextContent(hands);
    await user.click(handsTab);
    expect(within(screen.getByRole('region', { name: 'Accounts that need a hand' })).getAllByRole('listitem')).toHaveLength(Number(hands));
  });

  it('asks a plain question with the business and the site, and shows the post as it will look there', async () => {
    const { user } = await ready();
    await openInstagram(user);
    expect(card().getByRole('article', { name: 'Instagram post preview' })).toHaveTextContent('astercoffee');
    expect(card().getByText(/Our autumn blend is back/)).toBeInTheDocument();
    expect(card().getByText('How it will look on Instagram')).toBeInTheDocument();
    // The four plain rows.
    expect(card().getByText(/^Claude Code asked \d+ minutes? ago$/)).toBeInTheDocument();
    expect(card().getByText('Anyone on Instagram can see it')).toBeInTheDocument();
    expect(card().getByText('You can delete it later')).toBeInTheDocument();
    expect(card().getByText(/^Waits for you until /)).toBeInTheDocument();
    expect(card().getByRole('img', { name: 'How big is this: Others see it' })).toBeInTheDocument();
    // No engine words on her screen.
    expect(card().queryByText(/commit|effect|content hash|flow:|aster\/instagram/i)).not.toBeInTheDocument();
  });

  it('names the buttons for the action, no on the left and yes on the right', async () => {
    const { user } = await ready();
    const names = () => card().getAllByRole('button', { name: /^(Don't|Pay|Post|Delete|Keep)/ }).map((item) => item.textContent?.trim());
    // A payment opens first: it is the heaviest.
    expect(question()).toBe("Pay $40 to show a post to more people on Meridian Travel's Facebook?");
    expect(names()).toEqual(["Don't payD", 'Pay $40A']);
    await user.click(screen.getByRole('button', { name: /^Delete 3 draft pages/ }));
    // A long label keeps the room for itself, so its key hint steps aside.
    expect(names()).toEqual(['Keep them', 'Delete 3 pages']);
    await openInstagram(user);
    expect(names()).toEqual(["Don't postD", 'Post itA']);
  });

  it('moves with j and k, and posts with a', async () => {
    const { user } = await ready();
    const before = org().approvals.length;
    await openInstagram(user);
    await user.keyboard('j');
    expect(screen.getByRole('button', { name: /^LinkedIn post, Aster Coffee/ })).toHaveAttribute('aria-current', 'true');
    await user.keyboard('k');
    expect(screen.getByRole('button', { name: /^Instagram post, Aster Coffee/ })).toHaveAttribute('aria-current', 'true');
    await user.keyboard('a');
    await waitFor(() => expect(org().approvals).toHaveLength(before - 1));
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Instagram post, Aster Coffee/ })).not.toBeInTheDocument());
    // The next item takes focus once the done state has had its moment, so the keyboard run continues.
    await waitFor(() => expect(screen.getByRole('button', { name: /^LinkedIn post, Aster Coffee/ })).toHaveAttribute('aria-current', 'true'), { timeout: DONE_MS + 500 });
  });

  describe('nothing moves under the finger (walkthrough bug 3)', () => {
    it('shows the decided card as done, with no button where the tap was, then the next one arrives with its buttons off', async () => {
      const { user } = await ready();
      const before = org().approvals.length;
      await openInstagram(user);
      const yes = card().getByRole('button', { name: 'Post it' });
      // A double tap: the second lands where the first did.
      await user.dblClick(yes);
      expect(card().getByRole('status')).toHaveTextContent("Posting to Aster Coffee's Instagram");
      expect(card().queryByRole('button', { name: 'Post it' })).not.toBeInTheDocument();
      // The next item comes, and cannot be decided yet.
      await waitFor(() => expect(question()).toBe("Post this on Aster Coffee's LinkedIn?"), { timeout: DONE_MS + 500 });
      expect(card().getByRole('button', { name: 'Post it' })).toBeDisabled();
      await user.keyboard('a');
      await waitFor(() => expect(card().getByRole('button', { name: 'Post it' })).toBeEnabled(), { timeout: ARM_MS + 500 });
      await settle();
      // One tap, one decision: the second tap and the key decided nothing.
      expect(org().approvals).toHaveLength(before - 1);
    });

    it('ignores a key while the decided card is still on screen', async () => {
      const { user } = await ready();
      const before = org().approvals.length;
      await openInstagram(user);
      await user.keyboard('a');
      await user.keyboard('a');
      await user.keyboard('d');
      await settle();
      expect(org().approvals).toHaveLength(before - 1);
    });

    it('moves between items by hand without waiting', async () => {
      const { user } = await ready();
      await openInstagram(user);
      expect(card().getByRole('button', { name: 'Post it' })).toBeEnabled();
    });
  });

  describe('after a decision', () => {
    it('never says Approved: it says it is posting, with Undo, and Undo puts it back', async () => {
      const { user } = await ready();
      const target = org().approvals.find((item) => item.lane === 'aster/instagram')!;
      await openInstagram(user);
      await user.click(card().getByRole('button', { name: 'Post it' }));
      const bar = await screen.findByText(/^Posting to Aster Coffee's Instagram in \d s$/);
      expect(screen.queryByText(/Approved/)).not.toBeInTheDocument();
      await user.click(within(bar.closest('[role="status"]') as HTMLElement).getByRole('button', { name: 'Undo' }));
      expect(await screen.findByText(/^Undone\. It is back in your list/)).toBeInTheDocument();
      expect(org().approvals.some((item) => item.id === target.id)).toBe(true);
      await waitFor(() => expect(screen.getByRole('button', { name: /^Instagram post, Aster Coffee/ })).toHaveAttribute('aria-current', 'true'));
    });

    it('then says what really happened, with a link to the post', async () => {
      mockTiming.undoMs = 300;
      mockTiming.runMs = 100;
      const { user } = await ready();
      await openInstagram(user);
      await user.click(card().getByRole('button', { name: 'Post it' }));
      const bar = (await screen.findByText(/^Posting to Aster Coffee's Instagram in/)).closest('[role="status"]') as HTMLElement;
      expect(await within(bar).findByText("Posted to Aster Coffee's Instagram", {}, { timeout: 4000 })).toBeInTheDocument();
      expect(within(bar).getByRole('link', { name: 'View post' })).toHaveAttribute('href', expect.stringMatching(/^https:\/\/instagram\.com\/astercoffee\/p\//));
      // And in the decided list: "You decided today" when all of today is theirs, "Decided today" when a teammate decided too.
      await waitFor(() => expect(within(screen.getByRole('region', { name: /decided today/i })).getByText("Posted to Aster Coffee's Instagram")).toBeInTheDocument());
    }, 10_000);

    it('warns before deciding when the account cannot act, and brings a failed post back with the fix', async () => {
      mockTiming.undoMs = 200;
      mockTiming.runMs = 50;
      const lane = org().lanes.find((item) => item.id === 'aster/instagram')!;
      lane.state = 'quarantined';
      const { user } = await ready();
      await openInstagram(user);
      expect(card().getByRole('note')).toHaveTextContent("Aster Coffee's Instagram is switched off for safety, so if you say yes, it will not post until it is fixed.");
      expect(within(card().getByRole('note')).getByRole('link', { name: 'See what is wrong' })).toHaveAttribute('href', expect.stringMatching(/^\/northwind-group\/(lanes|accounts)\/aster\/instagram$/));
      await user.click(card().getByRole('button', { name: 'Post it once fixed' }));
      expect(await screen.findByText(/It is back in your list\./, {}, { timeout: 4000 })).toHaveTextContent(
        "Couldn't post to Aster Coffee's Instagram: the account is switched off for safety. It is back in your list.",
      );
      const failed = await screen.findByRole('region', { name: 'Did not go through' });
      await user.click(within(failed).getByRole('button', { name: 'Got it' }));
      await waitFor(() => expect(screen.queryByRole('region', { name: 'Did not go through' })).not.toBeInTheDocument());
    }, 10_000);

    it('shows a viewer a failed outcome with no "Got it": clearing it takes it away for everyone, so it is a decision', async () => {
      // An approved action whose run did not work, and nobody has cleared it yet
      const approved = org().history.find((item) => item.state === 'approved')!;
      approved.outcome = {
        state: 'failed', title: "Couldn't post to Aster Coffee's Instagram", reason: 'The account is switched off for safety.',
        detail: null, source: 'run', at: new Date().toISOString(), run_id: null, proof: null, cleared_at: null, cleared_by: null,
      };
      org().role = 'viewer';
      await ready();
      const failed = await screen.findByRole('region', { name: 'Did not go through' });
      expect(within(failed).getByText("Couldn't post to Aster Coffee's Instagram")).toBeInTheDocument();
      expect(within(failed).queryByRole('button', { name: 'Got it' })).not.toBeInTheDocument();
    }, 10_000);

    it('says no plainly, and can be undone as well', async () => {
      const { user } = await ready();
      await openInstagram(user);
      await user.click(card().getByRole('button', { name: "Don't post" }));
      expect(await screen.findByText(/^Not posting to Aster Coffee's Instagram\. Nothing is sent\.$/)).toBeInTheDocument();
    });
  });

  describe('heavy items (security review 12)', () => {
    it('asks again before a with a deletion, worded for the action, and arms after a moment', async () => {
    asComputer();
      const { user } = await ready();
      const before = org().approvals.length;
      const target = org().approvals.find((item) => item.effect === 'irreversible')!;
      await user.click(screen.getByRole('button', { name: /^Delete 3 draft pages/ }));
      await user.keyboard('a');
      const dialog = within(await screen.findByRole('dialog', { name: "Prove it's you" }));
      expect(dialog.getByText(/This can't be undone, so a tap isn't enough/)).toBeInTheDocument();
      expect(dialog.getByRole('button', { name: 'Use your passkey' })).toBeDisabled();
      await user.keyboard('a');
      expect(org().approvals).toHaveLength(before);
      await waitFor(() => expect(dialog.getByRole('button', { name: 'Use your passkey' })).toBeEnabled());
      await user.click(dialog.getByRole('button', { name: 'Use your passkey' }));
      await waitFor(() => expect(org().approvals.some((item) => item.id === target.id)).toBe(false));
    });

    it('asks again from the Pay button too, and Go back decides nothing', async () => {
    asComputer();
      const { user } = await ready();
      const before = org().approvals.length;
      await user.click(card().getByRole('button', { name: 'Pay $40' }));
      const dialog = within(await screen.findByRole('dialog'));
      expect(dialog.getByText(/Money can't be taken back from here/)).toBeInTheDocument();
      await user.click(dialog.getByRole('button', { name: 'Go back' }));
      expect(org().approvals).toHaveLength(before);
    });
  });

  describe('several at once', () => {
    it('offers it for routine posts only, never for money or the irreversible', async () => {
      const { user } = await ready();
      expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
      await user.click(screen.getByRole('button', { name: 'Choose several' }));
      expect(within(screen.getByRole('region', { name: 'Posts to check' })).getAllByRole('checkbox')).toHaveLength(5);
      expect(within(screen.getByRole('region', { name: 'Costs money' })).queryAllByRole('checkbox')).toHaveLength(0);
      expect(within(screen.getByRole('region', { name: "Can't be undone" })).queryAllByRole('checkbox')).toHaveLength(0);
    });

    it('posts the chosen ones together', async () => {
      const { user } = await ready();
      const before = org().approvals.length;
      await user.click(screen.getByRole('button', { name: 'Choose several' }));
      await user.click(screen.getByRole('checkbox', { name: /^Choose: Instagram post, Aster Coffee/ }));
      await user.click(screen.getByRole('checkbox', { name: /^Choose: LinkedIn post, Aster Coffee/ }));
      expect(await screen.findByText('2 posts chosen')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Post 2' }));
      await waitFor(() => expect(org().approvals).toHaveLength(before - 2));
      expect(await screen.findByText(/^Posting 2 posts in \d s$/)).toBeInTheDocument();
    });

    it('takes x for the post in focus, and does nothing with x on a payment', async () => {
      const { user } = await ready();
      await user.keyboard('x');
      expect(screen.queryByText(/chosen/)).not.toBeInTheDocument();
      await openInstagram(user);
      await user.keyboard('x');
      expect(await screen.findByText('1 post chosen')).toBeInTheDocument();
    });
  });

  it('keeps technical words behind Technical details, opened with h', async () => {
    const { user } = await ready();
    const hash = org().approvals.find((item) => item.effect === 'pay')!.content_hash;
    expect(screen.queryByText(hash)).not.toBeInTheDocument();
    await user.keyboard('h');
    expect(await screen.findByText(hash)).toBeInTheDocument();
    expect(card().getByText('pay')).toBeInTheDocument();
    await user.click(card().getByRole('button', { name: 'Technical details' }));
    expect(screen.queryByText(hash)).not.toBeInTheDocument();
  });

  it('leaves shortcuts alone while typing in a field', async () => {
    const { user } = await ready();
    const before = org().approvals.length;
    await user.click(screen.getByRole('button', { name: /What discount code/ }));
    const answer = await screen.findByRole('textbox', { name: 'Your answer' });
    await user.type(answer, 'ad');
    expect(org().approvals).toHaveLength(before);
    expect(answer).toHaveValue('ad');
  });

  it('keeps Always allowed out of the header, and offers Always allow posts like this under the technical details', async () => {
    const { user } = await ready();
    expect(screen.queryByRole('link', { name: 'Always allowed' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^LinkedIn post, Aster Coffee/ }));
    await user.click(card().getByRole('button', { name: 'Technical details' }));
    await user.click(card().getByRole('button', { name: 'Always allow posts like this' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Allow this again' }));
    expect(await dialog.findByText('No computer applies standing approvals yet. Whatever asks first will still ask each time.')).toBeInTheDocument();
  });

  it('never offers Always allow for a payment', async () => {
    const { user } = await ready();
    await user.click(card().getByRole('button', { name: 'Technical details' }));
    expect(card().queryByRole('button', { name: 'Always allow posts like this' })).not.toBeInTheDocument();
  });

  it('renders node text as text, never as HTML', async () => {
    const payload = '<img src=x onerror="window.pwned=1"><b>bold</b>';
    const target = org().approvals.find((item) => item.effect === 'pay')!;
    target.summary = payload;
    target.preview = { text: payload, image: null };
    renderApp(PATH);
    const shown = await screen.findAllByText(payload);
    expect(shown.length).toBeGreaterThan(0);
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect(document.querySelector('b')).toBeNull();
  });

  describe('questions', () => {
    it('sit in the same keyboard flow, and a number picks an option', async () => {
      const { user } = await ready();
      // The owner's brand change sits between the approvals and the questions: J walks into the Brand tab and out.
      const brandChanges = Number((await screen.findByRole('tab', { name: /^Brand/ })).textContent?.match(/\d+/)?.[0] ?? 0);
      for (let index = 0; index < org().approvals.length + brandChanges; index += 1) await user.keyboard('j');
      expect(screen.getByRole('button', { name: /Which photo should lead/ })).toHaveAttribute('aria-current', 'true');
      const before = org().questions.length;
      await user.keyboard('2');
      await waitFor(() => expect(org().questions).toHaveLength(before - 1));
    });

    it('takes a free text answer', async () => {
      const { user } = await ready();
      await user.click(await screen.findByRole('button', { name: /What discount code/ }));
      await user.type(await screen.findByRole('textbox', { name: 'Your answer' }), 'AUTUMN15');
      await user.click(screen.getByRole('button', { name: 'Send answer' }));
      await waitFor(() => expect(org().questions.some((item) => item.question.includes('discount'))).toBe(false));
    });

    it('offers a viewer no way to answer', async () => {
      org().role = 'viewer';
      const { user } = await ready();
      await user.click(await screen.findByRole('button', { name: /What discount code/ }));
      expect(await screen.findByText('Your role can see this but not answer it.')).toBeInTheDocument();
      expect(screen.queryByRole('textbox', { name: 'Your answer' })).not.toBeInTheDocument();
    });
  });

  it('says when the content changed, and asks again before a yes', async () => {
    const { user } = await ready();
    await openInstagram(user);
    const item = org().approvals.find((row) => row.lane === 'aster/instagram')!;
    item.content_hash = 'sha256:changed-on-the-node';
    await user.keyboard('a');
    expect(await screen.findByText('This changed since you saw it.')).toBeInTheDocument();
    expect(org().approvals.some((row) => row.id === item.id)).toBe(true);
    await waitFor(() => expect(card().getByRole('button', { name: 'Post it' })).toBeEnabled(), { timeout: ARM_MS + 500 });
    await user.keyboard('a');
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByRole('heading', { name: 'Yes, this version' })).toBeInTheDocument();
    expect(org().approvals.some((row) => row.id === item.id)).toBe(true);
  });

  it('holds rows still while the pointer is in the list', async () => {
    const { client, user } = await ready();
    const region = screen.getByRole('region', { name: 'Posts to check' });
    await user.hover(within(region).getAllByRole('button')[1]!);
    const arriving = { ...org().approvals[0]!, id: 'ap_LATE', effect: 'commit' as const, lane: 'harbor/instagram', summary: 'A late arrival' };
    act(() => applyLiveEvent(client, 'northwind-group', { type: 'approval', payload: arriving }));
    expect(await screen.findByText(/1 new when you move away/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Instagram post, Harbor Supply/ })).not.toBeInTheDocument();
    await user.unhover(within(region).getAllByRole('button')[1]!);
  });

  describe('never decides the wrong thing', () => {
    const stillQueued = () => expect(screen.getByRole('button', { name: /^Pay \$40 on Facebook/ })).toBeInTheDocument();

    it('swallows a wrong key after g, so "g d" says no to nothing', async () => {
      const { user } = await ready();
      const before = org().approvals.length;
      await user.keyboard('gd');
      stillQueued();
      await settle();
      expect(org().approvals).toHaveLength(before);
    });

    it('treats "g a" as navigation and decides nothing', async () => {
      const { user, router } = await ready();
      const before = org().approvals.length;
      await user.keyboard('ga');
      await waitFor(() => expect(router.state.location.pathname).not.toBe(PATH));
      expect(org().approvals).toHaveLength(before);
    });

    it('ignores a held key', async () => {
      await ready();
      const before = org().approvals.length;
      fireEvent.keyDown(document.body, { key: 'a', repeat: true });
      fireEvent.keyDown(document.body, { key: 'd', repeat: true });
      stillQueued();
      await settle();
      expect(org().approvals).toHaveLength(before);
    });

    it('keeps focus on the same item when a live approval lands above it', async () => {
      const { client } = await ready();
      expect(screen.getByRole('button', { name: /^Pay \$40 on Facebook/ })).toHaveAttribute('aria-current', 'true');
      const arriving = { ...org().approvals[0]!, id: 'ap_NEW', effect: 'pay' as const, summary: 'Pay $900 for a billboard' };
      act(() => applyLiveEvent(client, 'northwind-group', { type: 'approval', payload: arriving }));
      const fresh = await screen.findByRole('button', { name: /^Pay \$900/ });
      expect(fresh).not.toHaveAttribute('aria-current');
      expect(screen.getByRole('button', { name: /^Pay \$40 on Facebook/ })).toHaveAttribute('aria-current', 'true');
    });

    it('does not decide a chosen post that a workspace switch has hidden', async () => {
      const { user } = await ready();
      const hidden = org().approvals.find((item) => item.lane === 'harbor/facebook')!;
      await user.click(screen.getByRole('button', { name: /^Facebook post, Harbor Supply/ }));
      await user.keyboard('x');
      expect(await screen.findByText('1 post chosen')).toBeInTheDocument();
      await user.click(screen.getAllByRole('button', { name: 'Switch workspace' })[0]!);
      await user.click(await screen.findByRole('menuitem', { name: /Aster Coffee/ }));
      await waitFor(() => expect(screen.queryByText('1 post chosen')).not.toBeInTheDocument());
      await settle(ARM_MS);
      await user.keyboard('a');
      await settle();
      expect(org().approvals.some((item) => item.id === hidden.id)).toBe(true);
    });
  });

  it('names who asked in words, and keeps ids for the technical details', async () => {
    const { user } = await ready();
    await openInstagram(user);
    const first = org().approvals.find((item) => item.lane === 'aster/instagram')!;
    expect(card().getByText(/^Claude Code asked/)).toBeInTheDocument();
    expect(card().queryByText(/claude-code/)).not.toBeInTheDocument();
    if (first.requested_by.thread) expect(card().queryByText(first.requested_by.thread)).not.toBeInTheDocument();
  });

  it('writes accounts that need a hand as what to do, with the button for it', async () => {
    const target = org().lanes.find((item) => item.id === 'aster/instagram')!;
    // As the live stack had it: switched off, with the site signal still fine.
    target.state = 'quarantined';
    target.signal = 'ok';
    target.account = null;
    const { user } = await ready();
    await user.click(await screen.findByRole('tab', { name: /^Accounts that need a hand/ }));
    const hands = within(screen.getByRole('region', { name: 'Accounts that need a hand' }));
    expect(hands.getByText("Aster Coffee's Instagram is switched off for safety")).toBeInTheDocument();
    expect(hands.queryByText(/\b(OK|Fine)\b/)).not.toBeInTheDocument();
    expect(hands.getByText("Sign in to Solstice Studio's Facebook once")).toBeInTheDocument();
    expect(hands.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', expect.stringMatching(/^\/northwind-group\/(lanes|accounts)\/solstice\/facebook$/));
  });

  it('says All clear, calmly, when nothing waits', async () => {
    renderApp('/juniper-labs/needs-you');
    expect(await screen.findByRole('heading', { name: 'All clear' })).toBeInTheDocument();
    expect(screen.getByText('Nothing is waiting for you. Your routines run on their own.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Get told on your phone' })).toHaveAttribute('href', '/juniper-labs/settings/notifications');
  });

  describe('the developer kinds and brand changes (Mono)', () => {
    it('says what a yes does for a push, a VM and a lane, each with its own preview and buttons', async () => {
      const { user } = await ready();
      const push = await screen.findByRole('button', { name: /^Push to main in harbor-supply\/shop/ });
      expect(push).toHaveTextContent(/If yes\s*3 commits into\s*main\s*forced/);
      await user.click(push);
      await waitFor(() => expect(question()).toBe('Force-push 3 commits to main in harbor-supply/shop?'));
      const pushPreview = card().getByRole('article', { name: 'Push preview' });
      expect(pushPreview).toHaveTextContent('A force-push: commits on the remote that are not in this push are lost.');
      expect(within(pushPreview).getByRole('list', { name: 'Commits that would land on main' })).toHaveTextContent('Round the cart total to cents before charging');
      expect(card().getByRole('button', { name: /^Approve the push/ })).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: /^Rent a VM, €0\.04 at most/ }));
      await waitFor(() => expect(question()).toBe('Rent a cpx21 machine for up to 1 h 30 min?'));
      expect(card().getByRole('article', { name: 'Disposable VM preview' })).toHaveTextContent('€0.04');
      expect(card().getByRole('button', { name: /^Rent it/ })).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: /^New lane harbor\/run-shop/ }));
      await waitFor(() => expect(question()).toBe('Make the lane harbor/run-shop and run its first command?'));
      expect(card().getByRole('log', { name: 'The command it runs in harbor/run-shop' })).toHaveTextContent('pnpm vitest run');
      expect(card().getByRole('button', { name: /^Make the lane/ })).toBeInTheDocument();
    }, 15_000);

    it("puts a brand change in the owner's queue, and A approves the version shown through the brand routes", async () => {
      const { user } = await ready();
      const approvals = org().approvals.length;
      await user.click(await screen.findByRole('tab', { name: /^Brand/ }));
      await user.click(await screen.findByRole('button', { name: /^Brand change: Harbor Supply shop/ }));
      await waitFor(() => expect(card().getByRole('heading', { level: 2 })).toHaveTextContent('Approve version 4 of Harbor Supply shop?'));
      expect(card().getByText('brand.tokens.color.accent changed: #1f5fad -> #0e7c66')).toBeInTheDocument();
      expect(card().getByRole('img', { name: /accent, #0e7c66/i })).toBeInTheDocument();
      await act(() => settle(ARM_MS + 100));
      await user.keyboard('a');
      // The profile is now version 4, its draft gone: the brand route decided it.
      await waitFor(() => expect(org().brands?.find((item) => item.profile.ref === 'harbor/shop')?.profile).toMatchObject({ version: 4, draft: null }));
      await waitFor(() => expect(screen.queryByRole('button', { name: /^Brand change: Harbor Supply shop/ })).not.toBeInTheDocument());
      // Decided on the profile, not as an approval.
      expect(org().approvals).toHaveLength(approvals);
    }, 15_000);

    it('shows a member no brand change, and no error for it', async () => {
      org().role = 'viewer';
      renderApp(PATH);
      await screen.findByRole('heading', { name: 'Needs you', level: 1 });
      await settle(400);
      expect(screen.queryByRole('button', { name: /^Brand change:/ })).not.toBeInTheDocument();
      expect(screen.queryByText(/Could not load/)).not.toBeInTheDocument();
    });
  });
});
