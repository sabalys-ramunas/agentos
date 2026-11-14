/**
 * Resource shapes, exactly as docs/API.md and docs/WEB-NODE.md define them. Fields marked
 * "panel assumption" are not pinned by either document yet and are listed in DESIGN.md
 * so the API side can confirm them.
 *
 * One file per area in `types/`; import from here.
 */

export * from './types/access';
export * from './types/account';
export * from './types/approvals';
export * from './types/brand';
export * from './types/confirmations';
export * from './types/email';
export * from './types/environments';
export * from './types/fleet';
export * from './types/freeze';
export * from './types/history';
export * from './types/jobs';
export * from './types/lanes';
export * from './types/live';
export * from './types/logins';
export * from './types/memory';
export * from './types/metrics';
export * from './types/nodes';
export * from './types/org';
export * from './types/proof';
export * from './types/routines';
export * from './types/runs';
export * from './types/settings';
export * from './types/setup';
export * from './types/site-apis';
export * from './types/studio';
export * from './types/vms';
export * from './types/workers';
