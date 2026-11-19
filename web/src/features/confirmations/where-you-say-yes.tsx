/**
 * Where you can say yes: the places a person has, on the same ladder as the setting. A place reaches
 * as far as it can prove, so choosing a job shows at once which of them would do for it. Nothing here
 * adds a device: what is coming is said calmly, with no button to press.
 */
import { Link, useParams } from '@tanstack/react-router';
import { AppWindow, ArrowRight, Fingerprint, KeyRound, Laptop, MessageCircle, Smartphone, TabletSmartphone, Terminal, Watch } from 'lucide-react';
import type { CSSProperties } from 'react';
import { useState } from 'react';
import { useConfirmMethods, useConfirmation } from '@/api/hooks';
import type { ConfirmLevel, ConfirmMethodId, ConfirmMethodView } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { QueryView, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { METHOD_WORDS, reaches, rungOfLevel, RUNG_WORDS, rungsOf } from '@/lib/confirm';
import type { Rung } from '@/lib/confirm';
import { timeAgo } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { SettingsHeader } from '@/features/settings/page';
import { RungIcon } from './marks';
import './confirmations.css';

const ICONS: Record<ConfirmMethodId, typeof Laptop> = {
  chat: MessageCircle,
  panel: AppWindow,
  passkey: Fingerprint,
  mac: Laptop,
  cli: Terminal,
  ios: Smartphone,
  android: TabletSmartphone,
  watch: Watch,
  totp: KeyRound,
};

const LADDER: Exclude<Rung, 'auto'>[] = ['chat', 'signed_in', 'present'];
const GRID = 'grid gap-x-6 gap-y-2 md:grid-cols-[minmax(0,300px)_minmax(0,1fr)]';

interface Job {
  id: string;
  label: string;
  site: string;
  /** "a post needs", ready for a sentence. */
  noun: string;
  level: ConfirmLevel;
}

function placesWords(count: number, level: ConfirmLevel, noun: string) {
  const strength = RUNG_WORDS[rungOfLevel(level)].word.toLowerCase();
  const places = count === 1 ? 'one place' : `${count} places`;
  if (rungOfLevel(level) === 'present') return `${noun} takes proving it's you. You have ${places} that can do that.`;
  return `${noun} takes ${strength}. You have ${places} for that.`;
}

function MethodRow({ method, level }: { method: ConfirmMethodView; level: ConfirmLevel }) {
  const Icon = ICONS[method.id];
  const words = METHOD_WORDS[method.id];
  const enough = reaches(method.level, level);
  const reach = LADDER.indexOf(rungOfLevel(method.level));
  return (
    <li className={cn(GRID, 'relative items-center border-t border-line py-4')} data-short={enough ? undefined : ''}>
      <div className={cn('flex min-w-0 items-start gap-3 transition-opacity duration-(--duration-base) ease-standard', !enough && 'opacity-50')}>
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-line text-muted">
          <Icon aria-hidden="true" className="size-4" />
        </span>
        <span className="min-w-0">
          <span className="block text-label">{words.word}</span>
          <span className="block text-secondary text-pretty text-muted">{method.detail ?? words.feel}</span>
          <span className="tabular block text-caption text-faint">{method.last_used_at ? `Last used ${timeAgo(method.last_used_at).toLowerCase()}` : words.feel}</span>
        </span>
      </div>
      <div className="yes-reach md:-my-4 md:self-stretch" style={{ '--to': reach, '--need': LADDER.indexOf(rungOfLevel(level)) } as CSSProperties} role="img" aria-label={`Reaches ${RUNG_WORDS[rungOfLevel(method.level)].word}${enough ? '' : '. Not enough for this one'}`}>
        <span className="yes-reach-need hidden md:block" />
        <span className="yes-reach-bar" />
        <span className="yes-reach-end" />
      </div>
    </li>
  );
}

function Coming({ methods }: { methods: ConfirmMethodView[] }) {
  return (
    <section aria-label="Coming later" className="grid gap-4">
      <div>
        <h3 className="text-section-title">Coming later</h3>
        <p className="mt-1 text-secondary text-muted">Nothing to set up yet. When these arrive they fit the same ladder, and no rule has to change.</p>
      </div>
      <ul className="grid gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
        {methods.map((method) => {
          const Icon = ICONS[method.id];
          return (
            <li key={method.id} className="flex min-w-0 items-start gap-3">
              <Icon aria-hidden="true" className="mt-px size-4 shrink-0 text-faint" />
              <span className="min-w-0">
                <span className="block text-secondary font-medium text-muted">{METHOD_WORDS[method.id].word}</span>
                <span className="block text-caption text-pretty text-faint">{METHOD_WORDS[method.id].feel}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Page({ org, methods, jobs }: { org: string; methods: ConfirmMethodView[]; jobs: Job[] }) {
  const [technical] = useTechnical();
  const [jobId, setJobId] = useState(jobs[jobs.length - 1]?.id ?? '');
  const job = jobs.find((item) => item.id === jobId) ?? jobs[0];
  const yours = methods.filter((method) => method.status === 'ready' && method.yours && (technical || method.id !== 'cli'));
  const missing = methods.filter((method) => method.status === 'ready' && !method.yours);
  const later = methods.filter((method) => method.status === 'planned');
  if (!job) return null;
  const enough = yours.filter((method) => reaches(method.level, job.level));
  return (
    <div className="grid gap-10">
      <section aria-label="The places you have" className="grid gap-4">
        <div role="radiogroup" aria-label="Choose a job" className="grid gap-2 sm:grid-cols-3">
          {jobs.map((item) => (
            <button
              key={item.id}
              type="button"
              role="radio"
              aria-checked={item.id === job.id}
              onClick={() => setJobId(item.id)}
              className={cn(
                'flex min-w-0 items-center gap-3 rounded-xl border p-3 text-left transition-colors duration-(--duration-fast) ease-standard',
                item.id === job.id ? 'border-fg' : 'border-line hover:border-line-strong',
              )}
            >
              <SiteMark site={item.site} size={20} label={false} />
              <span className="min-w-0">
                <span className="block truncate text-secondary font-medium">{item.label}</span>
                <span className="flex items-center gap-1 text-caption text-faint">
                  <RungIcon rung={rungOfLevel(item.level)} className="size-3" />
                  {RUNG_WORDS[rungOfLevel(item.level)].word}
                </span>
              </span>
            </button>
          ))}
        </div>
        <p aria-live="polite" className="tabular text-heading text-balance">
          {placesWords(enough.length, job.level, job.noun)}
        </p>
        <div>
          <div className={cn(GRID, 'hidden md:grid')}>
            <span />
            <ol className="grid grid-cols-3 pb-2" aria-hidden="true">
              {LADDER.map((rung) => (
                <li key={rung} className={cn('flex items-center justify-center gap-2 text-secondary transition-colors duration-(--duration-base) ease-standard', rung === rungOfLevel(job.level) ? 'font-medium text-fg' : 'text-faint')}>
                  <RungIcon rung={rung} className="size-4" />
                  {RUNG_WORDS[rung].word}
                </li>
              ))}
            </ol>
          </div>
          <ul>
            {yours.map((method) => (
              <MethodRow key={method.id} method={method} level={job.level} />
            ))}
          </ul>
        </div>
        {missing.length > 0 ? (
          <p className="border-t border-line pt-4 text-secondary text-pretty text-muted">
            No Mac or iPhone? You don&apos;t need one. A chat covers the everyday, and a payment sends you a link that opens right here.
          </p>
        ) : null}
      </section>
      <section aria-label="When agentos is closed" className="grid gap-1 border-t border-line pt-6">
        <h3 className="text-section-title">When the app is closed</h3>
        <p className="max-w-[64ch] text-body text-pretty text-muted">
          With agentos Cloud, an alert reaches your Mac or phone even with the app closed. A computer with no cloud has nothing to carry it, so alerts arrive while the app is open. Nothing is lost either way: it waits in Needs you.
        </p>
      </section>
      <Coming methods={later} />
      <p className="text-secondary text-muted">
        <Link to="/$org/settings/yes" params={{ org }} className="inline-flex items-center gap-1 hover:text-fg">
          See what needs a yes
          <ArrowRight aria-hidden="true" className="size-4" />
        </Link>
      </p>
    </div>
  );
}

export function WhereYouSayYes() {
  const { org } = useParams({ from: '/$org/settings/say-yes' });
  const methods = useConfirmMethods(org);
  const setting = useConfirmation(org).data;
  const rungs = setting ? rungsOf(setting.setting) : null;
  const level = (rung: Rung | undefined, fallback: ConfirmLevel): ConfirmLevel => (!rung || rung === 'auto' ? fallback : rung);
  const jobs: Job[] = [
    // Kinds of job, never made-up ones that read like real work waiting on this computer (2026-11-15 test, F9).
    { id: 'post', label: 'For example, a post', site: 'instagram', noun: 'A post', level: level(rungs?.marked, 'chat') },
    { id: 'reply', label: 'For example, a reply after reading a page', site: 'tripadvisor', noun: 'A reply after outside words', level: level(rungs?.after_untrusted, 'signed_in') },
    { id: 'pay', label: 'For example, a payment', site: 'quickbooks', noun: 'A payment', level: level(rungs?.pay, 'present') },
  ];
  return (
    <>
      <SettingsHeader title="Where you can say yes" description="The stronger the yes, the fewer places can give it. Pick a kind of job to see which of yours would do. These are examples, not requests waiting for you." />
      <QueryView query={methods} card skeleton={<Skeleton className="h-96 rounded-xl" />}>
        {(data) => <Page org={org} methods={data} jobs={jobs} />}
      </QueryView>
    </>
  );
}
