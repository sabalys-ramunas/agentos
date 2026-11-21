/** An environment's or a command's state as a pill: its mark and word on a soft tint (a Badge with the status mark). */
import type { EnvState } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import type { BadgeTone } from '@/components/ui/badge';
import { StatusDot } from '@/components/ui/status';
import type { StatusLabel, Tone } from '@/lib/labels';
import { ENV_STATUS } from './words';

const BADGE: Record<Tone, BadgeTone> = { ok: 'ok', warn: 'warn', bad: 'bad', live: 'live', info: 'live', idle: 'neutral', neutral: 'neutral' };

export function StatusPill({ status }: { status: StatusLabel }) {
  return (
    <Badge tone={BADGE[status.tone]} className="gap-2 pr-3 pl-2">
      <StatusDot tone={status.tone} starting={status.starting} />
      {status.label}
    </Badge>
  );
}

export function EnvStatusPill({ state }: { state: EnvState }) {
  return <StatusPill status={ENV_STATUS[state]} />;
}
