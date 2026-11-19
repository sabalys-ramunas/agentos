/**
 * What a yes takes, said on the decision itself. A tap needs one quiet line. More than a tap gets a
 * strip above the buttons, with the reason, so the step up is never a surprise after the press.
 */
import { Fingerprint } from 'lucide-react';
import type { Approval, ConfirmMethodId } from '@/api/types';
import { METHOD_WORDS, needsProof, rungOfLevel, RUNG_WORDS, whyProof } from '@/lib/confirm';
import { RungIcon } from './marks';

const PROVES: ConfirmMethodId[] = ['passkey', 'mac', 'ios', 'android', 'watch', 'totp'];

/** "Your passkey or the Mac app": the places this person could really do it. */
function placesLine(methods: ConfirmMethodId[]) {
  const places = methods.filter((id) => PROVES.includes(id)).map((id) => METHOD_WORDS[id].word);
  const [first = 'Your passkey', ...rest] = places;
  const named = rest.length ? `${first} or ${rest.map((word) => word.replace(/^The /, 'the ')).join(' or ')}` : first;
  return `${named}, just now, for this one.`;
}

export function YesStep({ approval }: { approval: Approval }) {
  if (!approval.requires || !needsProof(approval)) return null;
  return (
    <div role="note" className="flex items-start gap-3 rounded-xl border border-line-strong p-4">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-hover">
        <Fingerprint aria-hidden="true" className="size-5" />
      </span>
      <span className="min-w-0">
        <span className="block text-label">Prove it&apos;s you to say yes</span>
        <span className="block text-secondary text-pretty text-muted">{whyProof(approval)}</span>
        <span className="block text-caption text-pretty text-faint">{placesLine(approval.methods ?? [])}</span>
      </span>
    </div>
  );
}

/** Under the buttons, when a tap will do. */
export function TapLine({ approval }: { approval: Approval }) {
  if (!approval.requires || needsProof(approval)) return null;
  const rung = rungOfLevel(approval.requires);
  const chat = approval.methods?.includes('chat');
  const words = rung === 'chat' ? (chat ? 'Just a tap. You can also answer this in your chat.' : 'Just a tap.') : `${RUNG_WORDS[rung].word}. A yes in chat doesn't count for this one.`;
  return (
    <p className="flex items-center justify-center gap-2 text-caption text-faint">
      <RungIcon rung={rung} className="size-3" />
      {words}
    </p>
  );
}
