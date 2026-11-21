/**
 * The first screen an invited approver sees (startup-invited.html): not Home, not numbers. What
 * they will be asked, with a picture of each kind, why it is safe, and one button to what is
 * waiting. On a phone the same, stacked, with the three steps to get asked there.
 */
import { Link, useParams, useSearch } from '@tanstack/react-router';
import { Card } from '@/components/ui/card';
import { Check, Clock, Hand, Lock, Smartphone } from 'lucide-react';
import { useState } from 'react';
import { useApprovals, useSession, useWorkspaces } from '@/api/hooks';
import { BusinessMark, PersonMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { AppTile, type TileTone } from '@/components/ui/tile';
import { Brand, DawnPicture } from './layout';
import { PhoneSteps } from '../onboarding/step-people';

/** "Aster Coffee", "Aster Coffee and Harbor Supply", or "Aster Coffee and 4 more". */
function names(list: string[]): string {
  if (list.length <= 1) return list[0] ?? '';
  if (list.length === 2) return `${list[0]} and ${list[1]}`;
  return `${list[0]} and ${list.length - 1} more`;
}

function Ask({ title, body, note }: { title: string; body: string; note: string }) {
  return (
    <li className="py-3 first:pt-0 last:pb-0">
      <p className="text-label text-fg">{title}</p>
      <p className="mt-1 text-secondary text-muted">{body}</p>
      <p className="mt-1 text-caption text-faint">{note}</p>
    </li>
  );
}

const FACT_TILES = { hand: Hand, check: Check, clock: Clock, lock: Lock } as const;

export function Welcome() {
  const { org } = useParams({ from: '/$org/welcome' });
  const { from } = useSearch({ from: '/$org/welcome' });
  const user = useSession().data?.user;
  const workspaces = useWorkspaces(org).data ?? [];
  const waiting = useApprovals(org).data?.length ?? 0;
  const [phone, setPhone] = useState(false);
  // A name the cloud made from the address is not a name to greet someone by.
  const named = user?.name && user.name !== user.email.split('@')[0] ? user.name : null;
  const first = named?.split(' ')[0] ?? null;
  const business = names(workspaces.map((item) => item.name)) || 'this business';
  const inviter = from || 'They';
  const main = workspaces[0];
  const facts: { tile: keyof typeof FACT_TILES; tone: TileTone; title: string; body: string }[] = [
    {
      tile: 'hand',
      tone: 'violet',
      title: 'You decide what asks first',
      body: `Payments and deletions always wait for you or ${inviter === 'They' ? 'the owner' : inviter}. So does any routine set to ask first.`,
    },
    { tile: 'check', tone: 'green', title: 'Saying no is always safe', body: 'It just stops that one thing. Nothing breaks.' },
    { tile: 'clock', tone: 'blue', title: 'No rush', body: 'If nobody answers, it waits, then quietly expires.' },
    { tile: 'lock', tone: 'ink', title: `You only see ${business}`, body: 'Everything else stays private.' },
  ];

  return (
    <div className="min-h-dvh bg-page">
      <header className="relative overflow-hidden px-4 pt-6 pb-32 sm:px-10 sm:pt-8 lg:pb-36">
        <DawnPicture />
        <div aria-hidden="true" className="scrim-welcome absolute inset-0" />
        <div className="relative mx-auto max-w-[1360px]">
          <div className="flex items-center justify-between gap-3">
            <Brand onPicture />
            {user ? (
              <span className="flex items-center gap-2 text-secondary text-(--sidebar-text-secondary)">
                <span className="hidden sm:inline">{user.name}</span>
                <PersonMark id={user.id} name={user.name} size={24} />
              </span>
            ) : null}
          </div>
          <div className="mt-12 max-w-[46em] sm:mt-16">
            {from ? <p className="text-body text-(--sidebar-text-secondary)">{from} invited you</p> : null}
            <h1 className="mt-2 text-page-title-compact text-balance text-(--sidebar-text) [text-shadow:0_1px_12px_rgb(4_6_24/0.25)] md:text-display">
              {first ? `Welcome, ${first}.` : 'Welcome.'}
            </h1>
            <p className="mt-4 text-lede text-(--sidebar-text-secondary)">
              You're helping {from ? `${from} with` : 'with'} {business}.{' '}
              Its agents work on computers of their own in the cloud: they run tests, check the pages they build, push branches, send emails. When something is
              set to ask first, or it costs money, they ask a person. That's you.
            </p>
          </div>
        </div>
      </header>
      <main className="relative mx-auto -mt-24 grid w-full max-w-[1440px] gap-6 px-4 pb-16 sm:px-10 lg:grid-cols-2">
        <Card role="region" aria-label="Good to know" className="p-6 sm:p-8">
          <ul className="grid gap-5">
            {facts.map((row) => (
              <li key={row.title} className="flex gap-4">
                <AppTile tone={row.tone} icon={FACT_TILES[row.tile]} size="sm" />
                <span className="min-w-0">
                  <span className="block text-card-title">{row.title}</span>
                  <span className="mt-1 block text-secondary text-muted">{row.body}</span>
                </span>
              </li>
            ))}
          </ul>
          <div className="mt-8 flex flex-wrap gap-3">
            <Button variant="primary" size="lg" asChild>
              <Link to="/$org/needs-you" params={{ org }}>
                {waiting ? "Show me what's waiting" : 'Go to Needs you'}
                {waiting ? <span className="tabular opacity-90">· {waiting}</span> : null}
              </Link>
            </Button>
            <Button size="lg" icon={<Smartphone className="size-4" />} aria-expanded={phone} onClick={() => setPhone(!phone)}>
              Get asked on my phone
            </Button>
          </div>
          {phone ? (
            <div className="mt-6 rounded-xl border border-line bg-inset p-4">
              <PhoneSteps org={org} />
            </div>
          ) : null}
        </Card>
        <Card role="region" aria-labelledby="asked-title" className="p-6 sm:p-8">
          <h2 id="asked-title" className="text-card-title">
            What you'll be asked
          </h2>
          <p className="mt-1 text-secondary text-muted">
            You see exactly what goes out: the email, the invoice, the form, the post. A yes sends exactly that. Not now sends nothing.
          </p>
          <div aria-hidden="true" className="mt-5 rounded-xl border border-line bg-inset p-4 shadow-lift">
            <div className="flex items-center gap-3">
              <BusinessMark name={main?.name ?? 'Aster Coffee'} color={main?.color} size={24} />
              <span className="min-w-0 flex-1 truncate text-label text-fg">{main?.name ?? 'Aster Coffee'}</span>
              <span className="text-secondary text-muted">Can this go out?</span>
            </div>
            <p className="mt-3 text-body text-fg">Our autumn blend is back.</p>
            <div className="mt-3 flex justify-end gap-2">
              <Button asChild size="sm">
                <span>Not now</span>
              </Button>
              <Button asChild size="sm" variant="primary">
                <span>Approve</span>
              </Button>
            </div>
          </div>
          <p className="mt-3 text-secondary text-faint">Only for routines set to ask you first.</p>
          <ul className="mt-6 grid divide-y divide-line border-t border-line pt-4">
            <Ask title="Can it spend money?" body="The amount and what it is for, before anything is paid." note="Every payment asks, every time" />
            <Ask
              title="A quick question"
              body="Sometimes it needs a choice only a person can make. Tap an answer, or type one."
              note={'"Which photo should lead Thursday\'s post?"'}
            />
          </ul>
        </Card>
      </main>
    </div>
  );
}
