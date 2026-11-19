/**
 * The first screen an invited approver sees (startup-invited.html): not Home, not numbers. What
 * they will be asked, with a picture of each kind, why it is safe, and one button to what is
 * waiting. On a phone the same, stacked, with the three steps to get asked there.
 */
import { Link, useParams, useSearch } from '@tanstack/react-router';
import { Clock, Eye, Hand, ShieldCheck, Smartphone } from 'lucide-react';
import { useState } from 'react';
import { useApprovals, useSession, useWorkspaces } from '@/api/hooks';
import { BusinessMark, PersonMark } from '@/components/brand/identity';
import { Count } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Logo } from '@/components/ui/misc';
import { ApprovalPicture, MoneyPicture, QuestionPicture } from '../onboarding/pictures';
import { PhoneSteps } from '../onboarding/step-people';

/** "Aster Coffee", "Aster Coffee and Harbor Supply", or "Aster Coffee and 4 more". */
function names(list: string[]): string {
  if (list.length <= 1) return list[0] ?? '';
  if (list.length === 2) return `${list[0]} and ${list[1]}`;
  return `${list[0]} and ${list.length - 1} more`;
}

function Ask({ picture, title, body, note }: { picture: React.ReactNode; title: string; body: string; note: string }) {
  return (
    <li className="grid gap-4 rounded-xl border border-line bg-card p-4 sm:grid-cols-[160px_minmax(0,1fr)] sm:items-center">
      <div className="w-full max-w-[220px] justify-self-center sm:max-w-none">{picture}</div>
      <div>
        <p className="text-section-title">{title}</p>
        <p className="mt-1 text-body text-muted">{body}</p>
        <p className="mt-2 text-secondary text-faint">{note}</p>
      </div>
    </li>
  );
}

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

  return (
    <div className="min-h-dvh bg-card">
      <header className="flex h-(--topbar-height) items-center gap-2 border-b border-line px-4">
        <Logo />
        {main ? (
          <>
            <span aria-hidden="true" className="text-faint">
              /
            </span>
            <BusinessMark name={main.name} color={main.color} size={20} />
            <span className="truncate text-label">{business}</span>
          </>
        ) : null}
        {user ? (
          <span className="ml-auto flex items-center gap-2 text-secondary text-muted">
            <span className="hidden sm:inline">{user.name}</span>
            <PersonMark id={user.id} name={user.name} size={24} />
          </span>
        ) : null}
      </header>
      <main className="mx-auto grid w-full max-w-6xl gap-10 px-4 pt-10 pb-16 sm:px-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)] lg:gap-16 lg:pt-16">
        <section>
          {from ? <p className="text-secondary text-faint">{from} invited you</p> : null}
          <h1 className="mt-2 text-headline text-balance">
            {first ? `Hi ${first}. ` : 'Hi. '}You're helping {from ? `${from} with` : 'with'} {business}.
          </h1>
          <p className="mt-4 text-lede text-muted">Its agents work on computers of their own in the cloud: they run tests, check the pages they build, push branches, send emails. When something is set to ask first, or it costs money, they ask a person. That's you.</p>
          <ul aria-label="Good to know" className="mt-8 grid gap-4">
            {[
              {
                icon: Hand,
                title: 'You decide what asks first',
                body: `Payments and deletions always wait for you or ${inviter === 'They' ? 'the owner' : inviter}. So does any routine set to ask first.`,
              },
              { icon: ShieldCheck, title: 'Saying no is always safe', body: 'It just stops that one thing. Nothing breaks.' },
              { icon: Clock, title: 'No rush', body: 'If nobody answers, it waits, then quietly expires.' },
              { icon: Eye, title: `You only see ${business}`, body: 'Everything else stays private.' },
            ].map((row) => (
              <li key={row.title} className="flex gap-3">
                <span aria-hidden="true" className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg border border-line bg-inset text-muted">
                  <row.icon className="size-4" />
                </span>
                <span>
                  <span className="block text-label">{row.title}</span>
                  <span className="block text-secondary text-muted">{row.body}</span>
                </span>
              </li>
            ))}
          </ul>
          <div className="mt-8 flex flex-wrap gap-2">
            <Button variant="primary" size="lg" asChild>
              <Link to="/$org/needs-you" params={{ org }}>
                {waiting ? "Show me what's waiting" : 'Go to Needs you'}
                {waiting ? <Count value={waiting} className="bg-card/20 text-on-primary" /> : null}
              </Link>
            </Button>
            <Button size="lg" icon={<Smartphone className="size-4" />} aria-expanded={phone} onClick={() => setPhone(!phone)}>
              Get asked on my phone
            </Button>
          </div>
          {phone ? (
            <div className="mt-6 rounded-xl border border-line p-4">
              <PhoneSteps org={org} />
            </div>
          ) : null}
        </section>
        <section aria-labelledby="asked-title">
          <h2 id="asked-title" className="text-label">
            What you'll be asked
          </h2>
          <ul className="mt-3 grid gap-3">
            <Ask
              picture={<ApprovalPicture business={main?.name ?? 'Aster Coffee'} small question="Send this?" caption="Our autumn blend is back." />}
              title="Can this go out?"
              body="You see exactly what goes out: the email, the invoice, the form, the post. A yes sends exactly that. Not now sends nothing."
              note="Only for routines set to ask you first"
            />
            <Ask picture={<MoneyPicture />} title="Can it spend money?" body="The amount and what it is for, before anything is paid." note="Every payment asks, every time" />
            <Ask
              picture={<QuestionPicture />}
              title="A quick question"
              body="Sometimes it needs a choice only a person can make. Tap an answer, or type one."
              note={'"Which photo should lead Thursday\'s post?"'}
            />
          </ul>
        </section>
      </main>
    </div>
  );
}
