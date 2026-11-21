/** Settings and its pages, and standing approvals. */
import {
  createRoute,
  lazyRouteComponent,
} from '@tanstack/react-router';
import { orgRoute } from './org';
import { text } from './root';

// Settings

export const settingsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/settings',
  component: lazyRouteComponent(() => import('@/features/settings/layout'), 'SettingsLayout'),
});

export const settingsIndexRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: '/',
  component: lazyRouteComponent(() => import('@/features/settings/layout'), 'SettingsIndex'),
});

function settingsPage<Path extends string>(path: Path, load: Parameters<typeof lazyRouteComponent>[0], name: string) {
  return createRoute({
    getParentRoute: () => settingsRoute,
    path,
    component: lazyRouteComponent(load as never, name as never),
  });
}

/** Which AI service and which computer. A key is never part of this, or of any address. */
const addServiceRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: '/brains/add',
  validateSearch: (search: Record<string, unknown>): { service?: string; computer?: string } => ({
    service: text(search.service),
    computer: text(search.computer),
  }),
  component: lazyRouteComponent(() => import('@/features/workers/add-service'), 'AddService'),
});

export const settingsRoutes = [
  settingsPage('/general', () => import('@/features/settings/general'), 'GeneralSettings'),
  settingsPage('/members', () => import('@/features/settings/members'), 'MembersSettings'),
  settingsPage('/invites', () => import('@/features/settings/members'), 'InvitesSettings'),
  settingsPage('/workspaces', () => import('@/features/settings/workspaces'), 'WorkspacesSettings'),
  settingsPage('/access', () => import('@/features/settings/workspace-access'), 'WorkspaceAccessSettings'),
  settingsPage('/grants', () => import('@/features/settings/grants'), 'GrantsSettings'),
  settingsPage('/notifications', () => import('@/features/settings/notifications'), 'NotificationSettingsScreen'),
  settingsPage('/webhooks', () => import('@/features/settings/webhooks'), 'WebhooksSettings'),
  settingsPage('/retention', () => import('@/features/settings/retention'), 'RetentionSettings'),
  settingsPage('/memory', () => import('@/features/memory/memory-page'), 'MemorySettings'),
  settingsPage('/billing', () => import('@/features/settings/billing'), 'BillingSettings'),
  settingsPage('/audit', () => import('@/features/settings/audit'), 'AuditSettings'),
  settingsPage('/danger', () => import('@/features/settings/general'), 'DangerSettings'),
  settingsPage('/yes', () => import('@/features/confirmations/what-needs-a-yes'), 'WhatNeedsAYes'),
  settingsPage('/say-yes', () => import('@/features/confirmations/where-you-say-yes'), 'WhereYouSayYes'),
  settingsPage('/brains', () => import('@/features/settings/brains'), 'BrainsSettings'),
  addServiceRoute,
  settingsPage('/workers', () => import('@/features/workers/workers-page'), 'WorkersSettings'),
  settingsPage('/vault', () => import('@/features/settings/vault'), 'VaultSettings'),
  settingsPage('/account', () => import('@/features/settings/account'), 'ProfileSettings'),
  settingsPage('/passkeys', () => import('@/features/settings/account'), 'PasskeySettings'),
  settingsPage('/devices', () => import('@/features/settings/account'), 'DeviceSettings'),
] as const;

export const standingRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: '/standing',
  validateSearch: (search: Record<string, unknown>): { show?: 'all' } => (search.show === 'all' ? { show: 'all' } : {}),
  component: lazyRouteComponent(() => import('@/features/standing/standing'), 'StandingSettings'),
});
