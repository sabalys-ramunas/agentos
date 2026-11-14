/**
 * Which role may do what, as the server decides it. Here it only decides what to show.
 * The server is the authority on every request, so a stale role can never grant anything.
 */
import type { Role } from '@/api/types';

export type Action =
  | 'org.update'
  | 'org.delete'
  | 'billing.manage'
  | 'members.update'
  | 'members.remove'
  | 'invites.read'
  | 'invites.manage'
  | 'audit.read'
  | 'workspaces.create'
  | 'workspaces.update'
  | 'workspaces.delete'
  | 'grants.manage'
  | 'nodes.manage'
  | 'nodes.remove'
  | 'lanes.watch'
  | 'lanes.operate'
  // Take over, hand back and stop an agent on an account: the owner alone, as the node refuses everyone else.
  | 'lanes.control'
  | 'approvals.decide'
  | 'webhooks.manage'
  | 'standing_approvals.create'
  | 'ask_first.on'
  | 'ask_first.off'
  | 'confirmation.update'
  | 'confirmation.money'
  | 'activity.export'
  // Proof: whether the log is intact, and read-only links out of it. The owner alone.
  | 'audit.share'
  // What agents handed to an AI model, and cancelling or resuming it. The owner alone.
  | 'brains.tasks'
  // Safety checks a person turned off for an account, and why. Shown in Technical details, to the owner alone.
  | 'overrides.read'
  // What is left to log in in one business: the owner's alone, as the node keeps the login queue.
  | 'workspaces.queue'
  | 'vault.manage'
  | 'vault.open'
  | 'browsers.manage'
  | 'workspaces.transfer'
  // Break glass. An admin freezes one account or workspace; the whole computer and a lift are the owner's.
  | 'freeze.account'
  | 'freeze.computer'
  | 'freeze.lift'
  // One-time login codes that a site asked for. Only the owner types them.
  | 'codes.answer'
  // An account's own limits, pace and site list: the owner's alone, as the node refuses everyone else.
  | 'lanes.pace'
  | 'lanes.limits'
  | 'lanes.sites'
  // Clearing what an agent read: the owner's, in person, never a member's.
  | 'taint.clear'
  // What agents remember: the owner in person reads, confirms, reviews and forgets it, and nobody else.
  | 'memory.read';

const RANK: Record<Role, number> = { owner: 4, admin: 3, member: 2, approver: 1, viewer: 0, auditor: 0 };

const VIEWER: Action[] = [];
const APPROVER: Action[] = [...VIEWER, 'approvals.decide'];
/**
 * Watching a lane live shows whatever its account shows: messages, settings, one-time codes. So
 * it goes with operating, never with approving or viewing (security review 30, decided).
 * An operator sees what is waiting but never says yes or no to it: that is the approver's and the admin's.
 */
// Ask me first on is stricter, so whoever operates accounts may. A viewer or approver only sees it.
const MEMBER: Action[] = [...VIEWER, 'lanes.watch', 'lanes.operate', 'webhooks.manage', 'ask_first.on'];
const ADMIN: Action[] = [
  ...MEMBER,
  'approvals.decide',
  'org.update',
  'members.update',
  'members.remove',
  'invites.read',
  'invites.manage',
  'audit.read',
  'workspaces.create',
  'workspaces.update',
  'workspaces.delete',
  'grants.manage',
  'nodes.manage',
  'nodes.remove',
  'standing_approvals.create',
  // Off lets a routine act without a person: owners and admins only, after confirming it is them.
  'ask_first.off',
  // How strong a yes posts need. What money needs is the owner's alone.
  'confirmation.update',
  // Removing a saved login, by name: admins and the owner. The secret is never shown to anyone.
  'vault.manage',
  // Break glass: an admin freezes one account or workspace.
  'freeze.account',
];
// Browsers, their connections and moving a workspace hold a secret or a whole company's setup: the owner alone.
const OWNER: Action[] = [
  ...ADMIN,
  'org.delete',
  'billing.manage',
  'activity.export',
  'audit.share',
  'brains.tasks',
  'overrides.read',
  'workspaces.queue',
  'confirmation.money',
  // The vault's lock and passphrase, and adding a saved login, are typed on the computer: the owner alone.
  'vault.open',
  'browsers.manage',
  'workspaces.transfer',
  'freeze.computer',
  'freeze.lift',
  'codes.answer',
  'lanes.pace',
  'lanes.limits',
  'lanes.sites',
  'lanes.control',
  'taint.clear',
  'memory.read',
];

const ROLE_ACTIONS: Record<Role, ReadonlySet<Action>> = {
  viewer: new Set(VIEWER),
  // Evidence only: it reads what a viewer reads, and decides, changes and operates nothing.
  auditor: new Set(VIEWER),
  approver: new Set(APPROVER),
  member: new Set(MEMBER),
  admin: new Set(ADMIN),
  owner: new Set(OWNER),
};

export const can = (role: Role, action: Action) => ROLE_ACTIONS[role].has(action);

/** May this role hand out that role, or change someone who holds it? Never above their own. */
export const atLeast = (actor: Role, other: Role) => RANK[actor] >= RANK[other];

/** Owners and admins run the whole org, so they cannot be limited to workspaces. */
export const isOrgWide = (role: Role) => role === 'owner' || role === 'admin';

export const ROLES: Role[] = ['owner', 'admin', 'member', 'approver', 'viewer', 'auditor'];
