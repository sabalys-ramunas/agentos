/**
 * The account's health line, and the sign-in upkeep. The health line answers "is it safe to use" from the
 * brief the node keeps, which probes nothing, and reads the full checklist only when something is wrong.
 * Each shows only when it has something to say.
 */
import { useLaneCheck, useLaneChecks, useLaneKeepalive } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Card } from '@/components/ui/card';
import { fixWords, plainDetail } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { AccountSection, Answer } from './account-section';
import { checkStatus, keepaliveSaysSomething, keepaliveSentence, nextVisitWords, visitWords } from './account-words';

/**
 * Whether the account is safe to use, shown only when it is not, or only with a warning: dark glass beside the
 * live screen on the night band, a card under it.
 */
export function SafeLine({ org, lane, glass = false }: { org: string; lane: Lane; glass?: boolean }) {
  const brief = useLaneChecks(org).data?.lanes.find((item) => item.lane === lane.id);
  const wrong = brief !== undefined && (brief.level === 'warn' || brief.level === 'fail' || brief.safe === false);
  const check = useLaneCheck(org, lane.workspace, lane.name, wrong).data;
  const [technical] = useTechnical();
  if (!brief || !wrong) return null;
  const status = checkStatus(brief);
  const notes = (check?.checks ?? []).filter((item) => item.level !== 'ok' && item.level !== 'unknown');
  const body = (
    <section aria-label="Is it safe to use" className="grid gap-3 px-5 py-4">
      {/* The notes say what is wrong. The summary is said only where the notes do not already say it, never twice. */}
      <Answer tone={status.tone} word={status.word}>
        {check && notes.length && notes.every((item) => check.summary.includes(item.detail)) ? null : check ? check.summary : 'Checking the account.'}
      </Answer>
      {notes.length ? (
        <ul className="grid gap-2 border-t border-line pt-3">
          {notes.map((item) => (
            <li key={item.id} className="text-secondary break-words">
              <span className="text-fg">{item.title}.</span> <span className="text-muted">{plainDetail(item.detail)}</span>
              {item.fix ? (
                <span className="block break-words text-faint">
                  {fixWords(item.fix).plain}
                  {technical ? <span className="mono mt-1 block text-caption break-all">{item.fix}</span> : null}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
  return glass ? <div className="glass-night rounded-xl">{body}</div> : <Card>{body}</Card>;
}

/** What keeps the sign-in warm, and when it last ran. Off with nothing before it, the section says nothing. */
export function KeepaliveSection({ org, lane }: { org: string; lane: Lane }) {
  const plan = useLaneKeepalive(org, lane.workspace, lane.name).data;
  if (!plan || !keepaliveSaysSomething(plan)) return null;
  const last = plan.last_visit;
  const next = nextVisitWords(plan);
  const tone = plan.status === 'signed_out' ? 'bad' : plan.status === 'challenged' ? 'warn' : plan.enabled ? 'ok' : 'idle';
  // "Off" is the switch. "Paused" is a switch that is on, held for now, the same word as a paused pace or limit.
  const word = plan.status === 'signed_out' ? 'Signed out' : plan.status === 'challenged' ? 'Asked a question' : plan.blocked ? 'Paused' : plan.enabled ? 'Keeping it warm' : 'Off';
  return (
    <AccountSection id="account-keepalive" title="Sign-in upkeep">
      <Answer tone={tone} word={word}>
        {keepaliveSentence(plan)}
      </Answer>
      {last || next ? (
        <div className="grid gap-1 text-secondary text-faint">
          {last ? <p className="text-muted">{visitWords(last)}</p> : null}
          {next ? <p>{next}</p> : null}
        </div>
      ) : null}
    </AccountSection>
  );
}
