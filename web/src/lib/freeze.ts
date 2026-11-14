/**
 * What a freeze covers and what it says, as one place. The node decides coverage (`agentos.freeze`),
 * and these read it the same way: a computer or mesh freeze covers everything, then a workspace, then
 * one account. The first freeze that reaches an account is the one it is shown under.
 */
import type { FreezeRecord, FreezeScope, FreezeStatus } from '@/api/types';
import { timeAgo } from './format';

/** The freeze that covers an account, widest first, or null. */
export function coveringFreeze(status: FreezeStatus | undefined, lane: string): FreezeRecord | null {
  if (!status) return null;
  const workspace = lane.split('/')[0] ?? '';
  const frozen = status.frozen;
  return (
    frozen.find((record) => record.scope === 'mesh') ??
    frozen.find((record) => record.scope === 'node') ??
    frozen.find((record) => record.scope === 'workspace' && record.target === workspace) ??
    frozen.find((record) => record.scope === 'lane' && record.target === lane) ??
    null
  );
}

/** The freeze on this computer as a whole, or null. */
export function computerFreeze(status: FreezeStatus | undefined): FreezeRecord | null {
  if (!status) return null;
  return status.frozen.find((record) => record.scope === 'node' || record.scope === 'mesh') ?? null;
}

/** What the freeze reaches, in words that finish "Agents are stopped on ...". */
export const SCOPE_WORDS: Record<FreezeScope, string> = {
  lane: 'this account',
  workspace: 'every account in this workspace',
  node: 'this machine',
  mesh: 'every machine',
};

/** Who froze it, in words. The node writes `owner`, `member:<id>`, `agent:<id>` or `push`, and never a name. */
export function frozenBy(record: Pick<FreezeRecord, 'by'>): string {
  if (record.by.startsWith('member:')) return 'a member';
  if (record.by.startsWith('agent')) return 'an agent';
  if (record.by === 'push') return 'a notification link';
  return 'the owner';
}

/** "Frozen 5m ago by the owner." Calm and short, for a badge's tooltip or a line under a title. */
export function frozenSentence(record: Pick<FreezeRecord, 'at' | 'by'>, now = Date.now()): string {
  return `Frozen ${timeAgo(record.at, now).replace(/^Just now$/, 'just now')} by ${frozenBy(record)}.`;
}

/** What a freeze says when the person gave no reason. The node needs some text, so this is sent instead. */
export const NO_REASON = 'no reason given';

/** The reason as the record keeps it. */
export function reasonOf(record: Pick<FreezeRecord, 'reason'>): string {
  return record.reason.trim() || NO_REASON;
}
