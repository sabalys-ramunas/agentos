/**
 * "Ask me first" as the cloud answers it (cloud `fleet/ask_first.py`, api-for-rebuild.md 20). Worked
 * out on the way out from the settings a computer holds, so a change shows everywhere at once.
 * `?mock=bare` is hetzner-1 on an older build with no switches, and `?mock=ask-first-refused` is a
 * computer that refuses the cloud turning a switch off.
 */
import type { AskFirstSetBy, Flow, LaneAskFirst, Role } from '@/api/types';
import { scenario } from './scenario';
import type { NodeRecord, OrgData } from './seed';

export interface AskFirstSetting {
  scope: 'flow' | 'lane';
  id: string;
  ask_first: boolean;
  set_by: AskFirstSetBy;
  set_at: string;
  /** Why it started on, when the computer decided that itself. */
  why?: string;
}

const ALWAYS = new Set<Flow['effect']>(['pay', 'irreversible', 'install_base']);
const QUIET = new Set<Flow['effect']>(['read', 'draft']);

export const ALWAYS_MESSAGE = 'Payments and things that cannot be undone always ask you first, whatever this switch says.';
export const NOT_SUPPORTED_MESSAGE = 'This computer cannot change Ask me first from here yet. Update it to a newer version, then try again.';
export const OFFLINE_MESSAGE = 'This computer is offline, so nothing changed. Try again when it is back.';
export const CEILING_MESSAGE =
  'This computer does not let the cloud turn Ask me first off, because the cloud may not approve actions there. Turn it off on the computer itself.';

export const refusesTurningOff = () => scenario.has('ask-first-refused');

/** A viewer or an approver sees a switch and never changes it. */
export const mayTurn = (role: Role, on: boolean) => (on ? role === 'member' || role === 'admin' || role === 'owner' : role === 'admin' || role === 'owner');

const nodeOf = (data: OrgData, lane: string): NodeRecord | undefined => {
  const home = data.lanes.find((item) => item.id === lane)?.node;
  return data.nodes.find((node) => node.id === home);
};

export const isSupported = (node: NodeRecord | undefined) => Boolean(node) && !(scenario.has('bare') && node?.id === 'hetzner-1');
export const isOnline = (node: NodeRecord | undefined) => Boolean(node) && node?.state !== 'down';

const settingOf = (data: OrgData, scope: AskFirstSetting['scope'], id: string) => data.askFirst.find((item) => item.scope === scope && item.id === id);

export function withAsk(data: OrgData, flow: Flow): Flow {
  const own = settingOf(data, 'flow', flow.id);
  const account = settingOf(data, 'lane', flow.lane);
  const node = nodeOf(data, flow.lane);
  const on = Boolean(own?.ask_first);
  const always = ALWAYS.has(flow.effect);
  const because = always
    ? 'always'
    : QUIET.has(flow.effect)
      ? null
      : on
        ? 'routine'
        : account?.ask_first
          ? 'account'
          : isSupported(node)
            ? null
            : 'computer';
  return {
    ...flow,
    ask_first: on,
    ask_first_by: own ? (own.set_by.kind === 'agent' ? 'agent' : 'person') : null,
    ask_first_set_by: own?.set_by ?? null,
    ask_first_set_at: own?.set_at ?? null,
    ask_first_why: own?.why ?? null,
    account_asks_first: Boolean(account?.ask_first),
    always_asks: always,
    asks_first: because !== null,
    asks_because: because,
    ask_first_supported: isSupported(node),
    ask_first_online: isOnline(node),
  };
}

export function laneAsk(data: OrgData, lane: string): LaneAskFirst {
  const setting = settingOf(data, 'lane', lane);
  const node = nodeOf(data, lane);
  return {
    lane,
    ask_first: Boolean(setting?.ask_first),
    asks_first: Boolean(setting?.ask_first) || !isSupported(node),
    ask_first_set_by: setting?.set_by ?? null,
    ask_first_set_at: setting?.set_at ?? null,
    ask_first_supported: isSupported(node),
    ask_first_online: isOnline(node),
  };
}

export function applyAsk(data: OrgData, scope: AskFirstSetting['scope'], id: string, on: boolean, userId: string) {
  const next: AskFirstSetting = { scope, id, ask_first: on, set_by: { kind: 'user', id: userId }, set_at: new Date().toISOString() };
  data.askFirst = [...data.askFirst.filter((item) => item.scope !== scope || item.id !== id), next];
}
