/**
 * Where a key goes, drawn. You, your computer and the AI service stand in a line, and agentos Cloud
 * sits to one side with no wire a key could take. The same picture says every stage: the key waiting
 * with you, the check running pink, the key kept on the computer, and where a refusal broke it.
 */
import { Key, Monitor, User } from 'lucide-react';
import './workers.css';

export type KeyAt = 'you' | 'computer' | 'kept';

function Box({ x, label, note, lit, children }: { x: number; label: string; note?: string; lit?: boolean; children: React.ReactNode }) {
  return (
    <g>
      <rect x={x} y={132} width={92} height={68} rx={10} className="key-node" data-lit={lit ? '' : undefined} />
      <foreignObject x={x} y={144} width={92} height={24}>
        <div className="grid h-6 place-items-center text-muted">{children}</div>
      </foreignObject>
      <text x={x + 46} y={186} className="key-label">{label}</text>
      {note ? <text x={x + 46} y={216} className="key-note">{note}</text> : null}
    </g>
  );
}

const KEY_X: Record<KeyAt, number> = { you: 62, computer: 206, kept: 206 };

export function KeyRoute({ computer, service, thing, keyAt, checking = false, refused = false }: { computer: string; service: string; thing: string; keyAt: KeyAt; checking?: boolean; refused?: boolean }) {
  const moved = keyAt !== 'you';
  return (
    <svg viewBox="0 0 420 236" role="img" aria-label={`You, then ${computer}, then ${service}. agentos Cloud stands to one side and never sees the ${thing}.`} className="block w-full">
      <rect x={132} y={8} width={156} height={52} rx={10} className="key-cloud" />
      <text x={210} y={30} className="key-label">agentos Cloud</text>
      <text x={210} y={46} className="key-note">Never sees the {thing}</text>
      <path d="M210 60 V132" className="key-wire" data-kind="quiet" />
      <text x={218} y={100} className="key-note" style={{ textAnchor: 'start' }}>the result only</text>

      <path d="M108 166 H164" className="key-wire" data-kind={moved ? 'yes' : undefined} />
      <path d="M256 166 H312" className="key-wire" data-kind={refused ? 'no' : keyAt === 'kept' ? 'yes' : undefined} />
      {checking ? <path d="M256 166 H312" pathLength={1} className="key-run" /> : null}

      <Box x={16} label="You" lit={keyAt === 'you'}>
        <User aria-hidden="true" className="size-4" />
      </Box>
      <Box x={164} label={computer} note={keyAt === 'kept' ? `${thing.charAt(0).toUpperCase()}${thing.slice(1)} kept here` : 'Your computer'} lit={moved}>
        <Monitor aria-hidden="true" className="size-4" />
      </Box>
      <Box x={312} label={service}>
        <span className="size-4 rounded-sm border border-line-strong" />
      </Box>

      <g className="key-token" style={{ transform: `translate(${KEY_X[keyAt]}px, 122px)` }}>
        <circle cx={8} cy={8} r={11} fill="var(--foreground)" />
        <foreignObject x={0} y={0} width={16} height={16}>
          <div className="grid size-4 place-items-center text-card">
            <Key aria-hidden="true" className="size-3" />
          </div>
        </foreignObject>
      </g>
    </svg>
  );
}
