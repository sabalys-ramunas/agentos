/**
 * A list, and what will happen to it. The plan leads, because "when does each person get this?" is the
 * question. Below it, the email as a person will get it with what the computer adds marked as such,
 * and who will never be written to. The note on the sender's own responsibility shows once.
 * A node keeps less than the concept: whatever comes back null is said plainly, never drawn.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowLeft, Ban, Check, Clock, Globe, Lock, MailX, Repeat, Shield } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useMailList, useUpdateMailList, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { ListPerson, MailList } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Switch } from '@/components/ui/toggle';
import { ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { plural } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { planDays } from './plan';
import { SendPlan } from './send-plan';
import { FromComputer, Rule } from './shared';
import { agoWords, countryName, firstName, inWords, SUPPRESSED_WORD } from './words';

const NOTE_SEEN = 'agentos.email.first-list-note';
const weekday = (at: number) => new Date(at).toLocaleDateString('en', { weekday: 'long' });
const STATE_WORD: Record<NonNullable<ListPerson['state']>, string> = { planned: 'Not written to yet', sent: 'Written to', followed: 'Followed up once', replied: 'Replied', unsubscribed: 'Unsubscribed' };
const stateWord = (state: ListPerson['state']) => (state ? STATE_WORD[state] : 'Not kept here yet');

/** A calm line where the node does not keep a thing yet. */
function NotKept({ children }: { children: ReactNode }) {
  return <p className="text-body text-pretty text-muted">{children}</p>;
}

/** Once, plainly, on a first list. It informs, and it does not stand in the way. */
function FirstListNote({ list, onClose }: { list: MailList; onClose: () => void }) {
  return (
    <section aria-label="Before your first list" className="mt-8 grid gap-4 rounded-(--radius-card) border border-line p-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
      <div className="min-w-0 max-w-3xl">
        <h2 className="text-section-title">A word before your first list</h2>
        <p className="mt-2 text-body text-pretty text-muted">
          These emails go out in your name, so you are the sender, and the rules are yours to follow. Rules on unsolicited mail differ by country. In the EU, writing to a named person can need their consent, and writing to a business usually needs less.
          {list.countries?.length ? ` This list has people in ${inWords(list.countries.map(countryName))}.` : null}
        </p>
        <p className="mt-2 text-body text-pretty text-muted">openajar keeps a record of where each address came from, adds an unsubscribe to every email and stops at your limit. It cannot tell you what the law allows where you sell. If you are unsure, ask someone who knows.</p>
      </div>
      <Button onClick={onClose}>Understood</Button>
    </section>
  );
}

function Chip({ children }: { children: ReactNode }) {
  return <span className="rounded-sm bg-pressed px-1 text-secondary text-fg">{children}</span>;
}

/** `{first name}` shows as a named chip, or as the person's own value when previewing them. */
function Merge({ text, person, source }: { text: string; person: ListPerson | null; source: string }) {
  const parts = text.split(/(\{[^}]+\})/);
  const value = (name: string) => (name === 'first name' ? firstName(person?.name ?? '') : name === 'company' ? person?.company : name === 'where it came from' ? source : null);
  return (
    <>
      {parts.map((part, index) => {
        const name = /^\{(.+)\}$/.exec(part)?.[1];
        if (!name) return part;
        return person ? <strong key={index} className="font-medium">{value(name)}</strong> : <Chip key={index}>{name}</Chip>;
      })}
    </>
  );
}

function Letter({ list }: { list: MailList }) {
  const [technical] = useTechnical();
  const [sample, setSample] = useState<number | null>(null);
  const { letter } = list;
  const people = list.people ?? [];
  if (!letter) {
    return (
      <section aria-labelledby="letter">
        <h2 id="letter" className="mb-3 text-heading">What each person gets</h2>
        <NotKept>This list has no letter of its own. Each routine that sends it writes its own message.</NotKept>
      </section>
    );
  }
  const person = sample === null || !people.length ? null : (people[sample % people.length] ?? null);
  const source = person?.source_word ?? '';
  return (
    <section aria-labelledby="letter">
      <div className="mb-3 flex items-center justify-between gap-4">
        <h2 id="letter" className="text-heading">What each person gets</h2>
        {people.length ? (
          <Button variant="ghost" size="sm" onClick={() => setSample(sample === null ? 0 : sample + 1)}>
            {person ? `Showing ${firstName(person.name)}, see the next` : 'See it for a real person'}
          </Button>
        ) : null}
      </div>
      <div className="rounded-(--radius-card) border border-line">
        <dl className="grid gap-1 border-b border-line px-4 py-3 text-secondary">
          <div className="flex gap-3"><dt className="w-14 shrink-0 text-faint">From</dt><dd className="min-w-0 truncate">{letter.sender} {list.address ? <span className="mono text-caption text-faint">{list.address}</span> : null}</dd></div>
          <div className="flex gap-3"><dt className="w-14 shrink-0 text-faint">Subject</dt><dd className="min-w-0"><Merge text={letter.subject} person={person} source={source} /></dd></div>
        </dl>
        <p className="px-4 py-4 text-body leading-6 whitespace-pre-wrap"><Merge text={letter.body} person={person} source={source} /></p>
        <div className="rounded-b-(--radius-card) border-t border-dashed border-line-strong bg-inset px-4 py-3">
          <p className="silk mb-2 flex items-center gap-1"><Lock aria-hidden="true" className="size-3" />Your computer adds this to every email</p>
          <p className="text-secondary text-pretty text-muted">
            {letter.sender}, {letter.postal}. <Merge text={letter.source_line} person={person} source={source} /> <span className="underline underline-offset-2">Unsubscribe</span> with one press, and you will not hear from us again.
          </p>
          {technical ? <p className="mono mt-2 text-caption break-all text-faint">List-Unsubscribe-Post: List-Unsubscribe=One-Click</p> : null}
        </div>
      </div>
    </section>
  );
}

function Suppression({ list }: { list: MailList }) {
  return (
    <section aria-labelledby="never">
      <h2 id="never" className="mb-1 text-heading">Never written to</h2>
      {list.suppressed ? (
        <>
          <p className="mb-3 text-body text-pretty text-muted">Every email is checked against this first, for every list and every routine of this business. Only a person can take someone off it.</p>
          <ul className="divide-y divide-line border-y border-line">
            {list.suppressed.map((person) => (
              <li key={person.id} className="flex items-center gap-3 py-2">
                <Ban aria-hidden="true" className="size-3 shrink-0 text-faint" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-body">{person.name} <span className="text-secondary text-faint">{person.company}</span></span>
                  <span className="block truncate text-caption text-faint">{SUPPRESSED_WORD[person.why]}, {agoWords(person.since)}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <NotKept>Your computer checks every send against the do-not-write list first, and does not name who is on it here. Only a person can take someone off it.</NotKept>
      )}
      <ul className="mt-4 divide-y divide-line">
        <Rule icon={<Lock aria-hidden="true" className="size-3" />}>{list.mailbox_limit === null ? 'One sending address, the real one, and never more a day than its limit.' : `One sending address, the real one, and no more than ${list.mailbox_limit} a day from it.`}</Rule>
        <Rule icon={<MailX aria-hidden="true" className="size-3" />}>An unsubscribe works at once, and for this whole business.</Rule>
        <Rule icon={<Globe aria-hidden="true" className="size-3" />}>Each address keeps a record of where it came from, and when.</Rule>
      </ul>
    </section>
  );
}

function People({ people }: { people: ListPerson[] | null }) {
  const [all, setAll] = useState(false);
  if (!people) {
    return (
      <section aria-labelledby="people">
        <h2 id="people" className="mb-3 text-heading">Who is on it</h2>
        <NotKept>The people are read in person from your computer when this list opens, and they did not come this time.</NotKept>
      </section>
    );
  }
  const shown = all ? people : people.slice(0, 8);
  return (
    <section aria-labelledby="people">
      <h2 id="people" className="mb-3 text-heading">Who is on it</h2>
      <div className="overflow-x-auto rounded-(--radius-card) border border-line">
        <table className="w-full text-secondary">
          <thead className="text-left text-caption text-faint">
            <tr className="border-b border-line">
              <th className="px-4 py-2 font-normal">Name</th>
              <th className="px-4 py-2 font-normal">Address</th>
              <th className="hidden px-4 py-2 font-normal md:table-cell">Where it came from</th>
              <th className="px-4 py-2 font-normal">So far</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {shown.map((person) => (
              <tr key={person.id}>
                <td className="px-4 py-2 whitespace-nowrap">{person.name} <span className="text-faint">{person.company}</span></td>
                <td className="mono px-4 py-2 text-caption whitespace-nowrap">{person.address}</td>
                <td className="hidden px-4 py-2 whitespace-nowrap text-muted md:table-cell">{person.source_word}</td>
                <td className="px-4 py-2 whitespace-nowrap text-muted">{stateWord(person.state)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {people.length > 8 ? <Button variant="ghost" size="sm" className="mt-2" onClick={() => setAll(!all)}>{all ? 'Show fewer' : `Show all ${people.length}`}</Button> : null}
    </section>
  );
}

function Body({ org, list }: { org: string; list: MailList }) {
  const update = useUpdateMailList(org, list.id);
  const workspace = useWorkspaces(org).data?.find((item) => item.id === list.workspace);
  const [pace, setPace] = useState(list.pace ?? 0);
  const [follow, setFollow] = useState(true);
  const [seen, setSeen] = useState(() => localStorage.getItem(NOTE_SEEN) === '1');
  const people = useMemo(() => list.people ?? [], [list.people]);
  const { mailbox_limit: limit, other_per_day: other, days } = list;
  // The plan is drawn only from what the node keeps. Without a pace, its days and the mailbox's room, there is none.
  const shape = list.pace !== null && days !== null && limit !== null && other !== null ? { days, limit, other } : null;
  const room = shape ? shape.limit - shape.other : 0;

  const planned = useMemo(() => people.filter((person) => person.state === 'planned'), [people]);
  // A list under way only has follow-ups left to place, each on its own day.
  const due = useMemo(() => people.filter((person) => person.state === 'sent').map((person, index) => ({ id: person.id, name: person.name, at: Date.now() + ((index % 3) + 1) * 86_400_000 })), [people]);
  const plan = useMemo(
    () => (limit !== null && other !== null && list.pace !== null ? planDays({ people: planned, pace, limit, other, start: Date.now() + 86_400_000, follow, due: follow ? due : [] }) : []),
    [planned, pace, limit, other, list.pace, follow, due],
  );

  const followUps = plan.reduce((sum, day) => sum + day.follow.length, 0);
  const starting = plan.find((day) => day.first.length);
  const lastFirst = plan.filter((day) => day.first.length).at(-1);
  const sending = list.state === 'sending';
  const written = people.filter((person) => person.state !== null && person.state !== 'planned').length;
  const replied = people.filter((person) => person.state === 'replied').length;
  const unsubscribed = people.filter((person) => person.state === 'unsubscribed').length;

  return (
    <div className="grid gap-10 pb-16">
      <div>
        <Link to="/$org/email/leads" params={{ org }} className="mb-6 inline-flex items-center gap-1 text-secondary text-muted hover:text-fg">
          <ArrowLeft className="size-3" /> Leads
        </Link>
        <header>
          <div className="mb-3 flex items-center justify-between gap-4">
            <p className="flex min-w-0 items-center gap-2 text-body text-muted">
              {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={20} /> : null}
              <span className="truncate">{workspace?.name} · {list.name}</span>
            </p>
            {list.state === null ? null : sending ? (
              <Button loading={update.isPending} onClick={() => update.mutate({ state: 'paused' })}>Pause</Button>
            ) : (
              <Button variant="primary" loading={update.isPending} onClick={() => update.mutate(list.pace === null ? { state: 'sending' } : { state: 'sending', pace })}>{list.state === 'paused' ? 'Carry on sending' : 'Start sending'}</Button>
            )}
          </div>
          {written ? (
            <h1 className="max-w-4xl text-headline text-balance">{written} written to, {plural(replied, 'reply', 'replies')}. <span className="text-faint">{plural(unsubscribed, 'person', 'people')} said no thanks.</span></h1>
          ) : (
            <h1 className="max-w-4xl text-headline text-balance">{plural(list.size, 'person', 'people')} on this list. {starting ? <span className="text-faint">The first {starting.first.length} would go {weekday(starting.at)}.</span> : null}</h1>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
            {list.state === null ? (
              <p className="text-secondary text-pretty text-muted">
                Routines send from this list{list.address ? <> through <span className="mono text-caption">{list.address}</span></> : null}. This computer cannot pause a list or change its pace yet.
              </p>
            ) : (
              <p className="text-secondary text-muted">{sending ? 'Sending' : list.state === 'paused' ? 'Paused' : 'Nothing has been sent'}{list.address ? <> from <span className="mono text-caption">{list.address}</span></> : null}</p>
            )}
            <FromComputer computers={[list.workspace === 'aster' ? 'home-mini' : 'hetzner-1']} />
          </div>
        </header>
        {list.first_list && !seen ? (
          <FirstListNote
            list={list}
            onClose={() => {
              localStorage.setItem(NOTE_SEEN, '1');
              setSeen(true);
            }}
          />
        ) : null}
      </div>

      {shape ? (
        <section aria-labelledby="plan" className="registered picture-stage grid gap-8 rounded-(--radius-card) border border-line p-6 lg:grid-cols-[minmax(0,1fr)_16rem]">
          <div className="min-w-0">
            <h2 id="plan" className="mb-3 text-heading text-balance">
              {lastFirst ? `One email each, finished by ${weekday(lastFirst.at)}` : followUps ? `Everyone has had one email, and at most ${plural(followUps, 'follow-up')} to come` : 'Everyone has been written to'}
            </h2>
            <SendPlan past={shape.days} plan={plan} limit={shape.limit} other={shape.other} />
          </div>
          <div className="grid content-start gap-6">
            {planned.length ? (
              <label className="grid gap-2">
                <span className="flex items-baseline justify-between text-secondary">
                  <span className="font-medium">New people a day</span>
                  <span className="tabular text-body">{pace}</span>
                </span>
                <input type="range" min={5} max={Math.max(5, room)} value={pace} onChange={(event) => setPace(Number(event.target.value))} onPointerUp={() => update.mutate({ pace })} className="plan-slider" />
                <span className="text-caption text-pretty text-faint">The mailbox has room for {room} a day, after its other mail. A slower start looks more like a person writing.</span>
              </label>
            ) : null}
            <div className="flex items-start gap-3">
              <Switch checked={follow} onCheckedChange={setFollow} aria-label="Follow up once if no reply in 3 days" />
              <span className="min-w-0 text-secondary text-pretty">
                <span className="font-medium">Follow up once if no reply in 3 days</span>
                <span className="mt-1 block text-caption text-faint">A reply or an unsubscribe stops it. {list.routine ? <Link to="/$org/email/routines/$routine" params={{ org, routine: list.routine.id }} className="link">See the routine</Link> : null}</span>
              </span>
            </div>
            <ul className="grid gap-2 text-caption text-muted">
              <li className="flex items-center gap-2"><Clock aria-hidden="true" className="size-3 text-faint" />Weekdays, working hours</li>
              <li className="flex items-center gap-2"><Shield aria-hidden="true" className="size-3 text-faint" />Stops at {shape.limit} a day</li>
              {list.routine ? <li className="flex items-center gap-2"><Repeat aria-hidden="true" className="size-3 text-faint" />{list.routine.name}</li> : null}
              {sending ? <li className="flex items-center gap-2"><Check aria-hidden="true" className="size-3 text-faint" />Sending now</li> : null}
            </ul>
          </div>
        </section>
      ) : (
        <section aria-labelledby="plan">
          <h2 id="plan" className="mb-3 text-heading">When each person gets it</h2>
          <NotKept>
            The send plan is kept by the routine that sends this list.{' '}
            {list.routine ? <Link to="/$org/email/routines/$routine" params={{ org, routine: list.routine.id }} className="link">See {list.routine.name}</Link> : null}
          </NotKept>
        </section>
      )}

      <div className="grid gap-10 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:gap-16">
        <Letter list={list} />
        <Suppression list={list} />
      </div>
      <People people={list.people} />
    </div>
  );
}

export function MailListPage() {
  const { org, list: id } = useParams({ from: '/$org/email/lists/$list' });
  const list = useMailList(org, id);
  if (isNotServed(list.error)) return <Card><NotLive /></Card>;
  if (list.error) return <ErrorState error={list.error} onRetry={() => void list.refetch()} />;
  if (!list.data) return <div className="grid gap-8"><Skeleton className="h-24 max-w-3xl rounded-lg" /><Skeleton className="h-72 rounded-(--radius-card)" /></div>;
  return <Body org={org} list={list.data} />;
}
