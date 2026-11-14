/**
 * Step 3: which accounts will it use? (startup.md, `#s3`). Sites come from the first job, each
 * with its brand mark. "What it may do" is the grant ceiling in a person's words: on means
 * the AI may do it, on its own for sending and asking every time for money and deleting; off means
 * it cannot do it at all. The ceiling is a
 * ladder (posting, then spending, then deleting), so the switches move together where they must.
 */
import { useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { Plus, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { fleet } from '@/api/client';
import { useNodes } from '@/api/hooks';
import { ApiError } from '@/api/http';
import { keys } from '@/api/keys';
import type { Effect } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/field';
import { Checkbox, Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { describeError } from '@/lib/errors';
import { slugify } from '@/lib/format';
import { hostOf, siteName, siteOf } from '@/lib/sites';
import type { StepContext } from './context';
import { StepFooter } from './context';
import { Ready, SetupLayout } from './layout';
import type { SetupAsks } from './setup';
import { DEFAULT_ASKS, remember, useRefreshSetup } from './setup';

/** The plain switches, lowest first. Each one on includes the ones above it. */
const ASKS: { key: keyof SetupAsks; label: string; yes: string }[] = [
  { key: 'post', label: 'Send, post and submit things', yes: 'It does this on its own. You can make a routine or an account ask you first.' },
  { key: 'pay', label: 'Spend money', yes: 'It asks you first, every time.' },
  { key: 'delete', label: 'Delete things', yes: 'It asks you first, every time.' },
];

/** The grant ceiling the switches mean. Drafts and reading never need anyone. */
export function ceilingOf(asks: SetupAsks): Effect {
  if (asks.delete) return 'irreversible';
  if (asks.pay) return 'pay';
  if (asks.post) return 'commit';
  return 'draft';
}

/** Turning one on turns on the ones below it; turning one off turns off the ones above. */
export function setAsk(asks: SetupAsks, key: keyof SetupAsks, on: boolean): SetupAsks {
  const index = ASKS.findIndex((item) => item.key === key);
  const next = { ...asks };
  ASKS.forEach((item, position) => {
    if (on && position <= index) next[item.key] = true;
    if (!on && position >= index) next[item.key] = false;
  });
  return next;
}

/** A site key from what a person types: "etsy", "etsy.com", "https://www.etsy.com/shop/x". */
function siteKey(text: string): string {
  const known = siteOf(text.trim());
  if (known) return known.key;
  const host = hostOf(text);
  return slugify(host.replace(/\.[a-z]+$/, '') || text);
}

export function AccountsStep({ ctx }: { ctx: StepContext }) {
  const { org, setup, goal, workspace, business } = ctx;
  const client = useQueryClient();
  const refresh = useRefreshSetup(org);
  const nodes = useNodes(org).data ?? [];
  const computer = nodes[0] ?? null;
  const [picked, setPicked] = useState<string[] | null>(null);
  const [extra, setExtra] = useState<string[]>([]);
  const [asks, setAsks] = useState<SetupAsks | null>(null);
  const [adding, setAdding] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chosen = picked ?? setup?.sites ?? goal?.sites ?? [];
  const shownAsks = asks ?? setup?.asks ?? goal?.asks ?? DEFAULT_ASKS;
  const lanes = workspace?.lanes ?? [];
  const offered = [...new Set([...chosen, ...(setup?.sites ?? []), ...(goal?.sites ?? []), ...(goal?.offer ?? []), ...extra, ...lanes.map((lane) => lane.name)])];
  const laneFor = (site: string) => lanes.find((lane) => lane.name === site) ?? null;
  const missing = chosen.filter((site) => !laneFor(site));
  const loggedIn = chosen.filter((site) => laneFor(site)?.logged_in_hint === 'ok').length;

  const toggle = (site: string, on: boolean) => setPicked(on ? [...chosen, site] : chosen.filter((item) => item !== site));

  const addTyped = () => {
    const key = siteKey(typed);
    if (!key) return;
    setExtra([...extra, key]);
    setPicked([...chosen.filter((item) => item !== key), key]);
    setTyped('');
    setAdding(false);
  };

  /** Each new account gets its own private browser on the computer. */
  const makeAccounts = async () => {
    if (!computer || !workspace) return;
    for (const site of missing) {
      try {
        await fleet.createLane(org, { id: `${workspace.id}/${site}`, kind: 'browser', preset: 'account' });
      } catch (failure) {
        if (!(failure instanceof ApiError && failure.status === 409)) throw failure;
      }
    }
    await client.invalidateQueries({ queryKey: keys.workspaces(org) });
  };

  const save = async (andNext: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await makeAccounts();
      remember(org, { sites: chosen, asks: shownAsks });
      refresh();
      if (andNext) ctx.go('ai');
    } catch (failure) {
      setError(describeError(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SetupLayout
      org={org}
      business={business}
      step="accounts"
      setup={setup}
      eyebrow={computer ? <Ready>{computerName(computer.id)} is ready</Ready> : undefined}
      title="Which accounts will it use?"
      description={`Each account gets its own private browser on ${computer ? computerName(computer.id) : 'your computer'}. You log in yourself, once. Your AI never sees the password.`}
      hints={{ accounts: chosen.length ? chosen.map(siteName).join(', ') : undefined }}
      footer={
        <StepFooter
          ctx={ctx}
          step="accounts"
          note={chosen.length ? `${chosen.length} ${chosen.length === 1 ? 'account' : 'accounts'} · ${loggedIn} logged in` : 'Pick at least one, or add any website.'}
          later={false}
          busy={busy}
          onContinue={() => void save(true)}
        />
      }
    >
      <div className="grid gap-8">
        <ul aria-label="Accounts" className="divide-y divide-line overflow-hidden rounded-xl border border-line">
          {offered.map((site) => {
            const lane = laneFor(site);
            const on = chosen.includes(site);
            const ready = lane?.logged_in_hint === 'ok';
            const id = `site-${site}`;
            return (
              <li key={site} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <Checkbox id={id} checked={on} onCheckedChange={(value) => toggle(site, value === true)} />
                <SiteMark site={site} size={32} tile label={false} />
                <label htmlFor={id} className="min-w-0 flex-1 cursor-pointer">
                  <span className="block text-14 font-medium">{siteName(site)}</span>
                  <span className="block text-13 text-muted">
                    {ready
                      ? 'Logged in'
                      : lane
                        ? 'Ready for you to log in'
                        : !on
                          ? 'Optional'
                          : computer
                            ? `Gets its own browser on ${computerName(computer.id)}`
                            : 'Waits for your computer'}
                  </span>
                </label>
                {ready ? (
                  <Badge className="border-(--ok-soft) bg-(--ok-soft) text-fg">Ready</Badge>
                ) : lane && on ? (
                  <Button size="sm" asChild>
                    <Link to="/$org/lanes/$ws/$name" params={{ org, ws: lane.workspace, name: lane.name }} target="_blank">
                      Log in now
                    </Link>
                  </Button>
                ) : null}
              </li>
            );
          })}
          <li className="px-4 py-3">
            {adding ? (
              <form
                className="flex flex-wrap items-center gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  addTyped();
                }}
              >
                <Input autoFocus aria-label="Website" placeholder="etsy.com, or any site you use in a browser" className="h-9 min-w-0 flex-1" value={typed} onChange={(event) => setTyped(event.target.value)} />
                <Button type="submit" size="md" disabled={!typed.trim()}>
                  Add
                </Button>
                <Button variant="ghost" size="md" onClick={() => setAdding(false)}>
                  Cancel
                </Button>
              </form>
            ) : (
              <button type="button" onClick={() => setAdding(true)} className="flex w-full items-center gap-3 text-left">
                <span aria-hidden="true" className="inline-flex size-8 items-center justify-center rounded-lg border border-dashed border-line-strong text-faint">
                  <Plus className="size-4" />
                </span>
                <span>
                  <span className="block text-14 font-medium">Add another website</span>
                  <span className="block text-13 text-muted">Any site you can use in a browser</span>
                </span>
              </button>
            )}
          </li>
        </ul>

        {computer && missing.length ? (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-subtle px-4 py-3 text-14">
            <span className="mr-auto text-muted">Make {missing.length === 1 ? 'this account' : 'these accounts'} now, then log in to each one in its own window.</span>
            <Button size="md" loading={busy} onClick={() => void save(false)}>
              Make {missing.length === 1 ? 'it' : `${missing.length} accounts`}
            </Button>
          </div>
        ) : null}

        <section aria-labelledby="asks-title" className="grid gap-3">
          <h2 id="asks-title" className="text-14 font-medium">
            What it may do
          </h2>
          <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
            {ASKS.map((item) => {
              const on = shownAsks[item.key];
              return (
                <li key={item.key} className="flex items-center gap-4 px-4 py-3">
                  <label htmlFor={`ask-${item.key}`} className="min-w-0 flex-1 cursor-pointer">
                    <span className="block text-14">{item.label}</span>
                    <span className={cn('block text-13', on ? 'text-muted' : 'text-faint')}>{on ? item.yes : 'It cannot do this at all.'}</span>
                  </label>
                  <Switch id={`ask-${item.key}`} checked={on} onCheckedChange={(value) => setAsks(setAsk(shownAsks, item.key, value))} />
                </li>
              );
            })}
          </ul>
          <p className="flex items-center gap-2 text-13 text-muted">
            <ShieldCheck className="size-4 shrink-0 text-faint" aria-hidden="true" />
            Reading and writing drafts never needs you. Money and things that can't be undone always ask.
          </p>
        </section>

        {error ? (
          <p role="alert" className="text-13 text-red">
            {error}
          </p>
        ) : null}
      </div>
    </SetupLayout>
  );
}
