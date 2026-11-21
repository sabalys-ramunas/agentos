/**
 * Where a password goes, drawn. You, your computer and the mail company stand in a line, and openajar
 * Cloud sits off to one side with no wire a password could take. The same picture says every state:
 * looking, the password on its way, the two checks, and exactly where a failure broke.
 */
import { Key, Monitor, User } from 'lucide-react';
import { SiteMark } from '@/components/brand/site-mark';
import type { Check } from './words';

export type KeyAt = 'none' | 'you' | 'computer' | 'kept';

const WIRE: Record<Check, string | undefined> = { waiting: undefined, running: undefined, yes: 'yes', no: 'no', stopped: 'stopped' };

function Box({ x, label, note, lit, children }: { x: number; label: string; note?: string; lit?: boolean; children: React.ReactNode }) {
  return (
    <g>
      <rect x={x} y={132} width={92} height={68} rx={10} className="route-node" data-lit={lit ? '' : undefined} />
      <foreignObject x={x} y={144} width={92} height={24}>
        <div className="grid h-6 place-items-center text-muted">{children}</div>
      </foreignObject>
      <text x={x + 46} y={186} className="route-label">{label}</text>
      {note ? <text x={x + 46} y={216} className="route-note">{note}</text> : null}
    </g>
  );
}

const KEY_X: Record<Exclude<KeyAt, 'none'>, number> = { you: 62, computer: 206, kept: 206 };

export function RoutePicture({ computer, provider, site, looking = false, keyAt = 'none', read = 'waiting', send = 'waiting', lit }: {
  computer: string;
  provider: string | null;
  site: string | null;
  looking?: boolean;
  keyAt?: KeyAt;
  read?: Check;
  send?: Check;
  lit?: 'you' | 'computer' | 'mail';
}) {
  const name = provider ?? 'Your mail';
  const summary = `You, then ${computer}, then ${name}. openajar Cloud stands to one side and never sees the password.`;
  const at = keyAt === 'none' ? null : KEY_X[keyAt];
  return (
    <svg viewBox="0 0 420 236" role="img" aria-label={summary} className="block w-full">
      <defs>
        <pattern id="route-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="6" height="6" fill="var(--background)" />
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--fill-3)" strokeWidth="1" />
        </pattern>
      </defs>

      {/* The cloud hears a start and a result, on a dotted wire. No password fits down it. */}
      <rect x={132} y={8} width={156} height={52} rx={10} className="route-cloud" />
      <text x={210} y={30} className="route-label">openajar Cloud</text>
      <text x={210} y={46} className="route-note">Never sees the password</text>
      <path d="M210 60 V132" className="route-wire" data-kind="quiet" />
      <text x={218} y={100} className="route-note" style={{ textAnchor: 'start' }}>the result only</text>

      {/* You to your computer: the one way a password travels. */}
      <path d="M108 166 H164" className="route-wire" data-kind={keyAt === 'computer' || keyAt === 'kept' ? 'yes' : undefined} />

      {/* Your computer to the mail company: one wire to read, one to send. */}
      <path d="M256 156 H312" className="route-wire" data-kind={WIRE[read]} />
      <path d="M256 176 H312" className="route-wire" data-kind={WIRE[send]} />
      {looking ? <path d="M256 166 H312" pathLength={1} className="route-run" /> : null}
      {read === 'running' ? <path d="M256 156 H312" pathLength={1} className="route-run" /> : null}
      {send === 'running' ? <path d="M256 176 H312" pathLength={1} className="route-run" /> : null}
      <text x={284} y={150} className="route-note">read</text>
      <text x={284} y={190} className="route-note">send</text>

      <Box x={16} label="You" lit={lit === 'you'}>
        <User aria-hidden="true" className="size-4" />
      </Box>
      <Box x={164} label={computer} note={keyAt === 'kept' ? 'Password kept here' : 'Your computer'} lit={lit === 'computer'}>
        <Monitor aria-hidden="true" className="size-4" />
      </Box>
      <Box x={312} label={name.replace('Google ', '')} lit={lit === 'mail'}>
        {site ? <SiteMark site={site} size={16} label={false} /> : <span className="size-4 rounded-sm border border-dashed border-line-strong" />}
      </Box>

      {at !== null ? (
        <g className="route-key" style={{ transform: `translate(${at}px, 122px)` }}>
          <circle cx={8} cy={8} r={11} fill="var(--foreground)" />
          <foreignObject x={0} y={0} width={16} height={16}>
            <div className="grid size-4 place-items-center text-card">
              <Key aria-hidden="true" className="size-3" />
            </div>
          </foreignObject>
        </g>
      ) : null}
    </svg>
  );
}
