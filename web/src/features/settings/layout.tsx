import { Card } from '@/components/ui/card';
import { Link, Outlet, useLocation, useNavigate, useParams } from '@tanstack/react-router';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { useAccess } from '@/api/hooks';
import { cn } from '@/lib/cn';
import type { Action } from '@/lib/permissions';
import { THINGS } from '@/lib/vocabulary';

type SettingsPath =
  | '/$org/settings/general'
  | '/$org/settings/members'
  | '/$org/settings/invites'
  | '/$org/settings/workspaces'
  | '/$org/settings/access'
  | '/$org/settings/grants'
  | '/$org/settings/notifications'
  | '/$org/settings/webhooks'
  | '/$org/settings/standing'
  | '/$org/settings/yes'
  | '/$org/settings/say-yes'
  | '/$org/settings/retention'
  | '/$org/settings/memory'
  | '/$org/settings/billing'
  | '/$org/settings/audit'
  | '/$org/settings/danger'
  | '/$org/settings/brains'
  | '/$org/settings/workers'
  | '/$org/settings/vault'
  | '/$org/settings/account'
  | '/$org/settings/passkeys'
  | '/$org/settings/devices';

const SECTIONS: { title: string; items: { label: string; to: SettingsPath; need?: Action }[] }[] = [
  {
    title: 'Organization',
    items: [
      { label: 'General', to: '/$org/settings/general' },
      { label: 'Members', to: '/$org/settings/members' },
      { label: 'Invites', to: '/$org/settings/invites', need: 'invites.read' },
      { label: 'Workspaces', to: '/$org/settings/workspaces' },
      { label: 'Workspace access', to: '/$org/settings/access', need: 'members.update' },
      { label: 'Grants', to: '/$org/settings/grants', need: 'grants.manage' },
      { label: 'What needs a yes', to: '/$org/settings/yes' },
      { label: 'Standing approvals', to: '/$org/settings/standing' },
      { label: 'Notifications', to: '/$org/settings/notifications' },
      { label: 'Webhooks', to: '/$org/settings/webhooks', need: 'webhooks.manage' },
      { label: 'Billing and plan', to: '/$org/settings/billing', need: 'org.update' },
      { label: 'Retention and export', to: '/$org/settings/retention' },
      { label: 'Memory', to: '/$org/settings/memory', need: 'memory.read' },
      { label: 'Audit log', to: '/$org/settings/audit', need: 'audit.read' },
      { label: 'Danger zone', to: '/$org/settings/danger', need: 'org.delete' },
    ],
  },
  {
    title: 'Fleet',
    items: [
      { label: THINGS.brain.word, to: '/$org/settings/brains' },
      { label: THINGS.plugin.word, to: '/$org/settings/workers' },
      { label: 'Vault', to: '/$org/settings/vault' },
    ],
  },
  {
    title: 'Account',
    items: [
      { label: 'Profile', to: '/$org/settings/account' },
      { label: 'Where you can say yes', to: '/$org/settings/say-yes' },
      { label: 'Passkeys', to: '/$org/settings/passkeys' },
      { label: 'Devices', to: '/$org/settings/devices' },
    ],
  },
];

/** Only what this person can use. A section they cannot open is not worth showing. */
function useSections(org: string) {
  const access = useAccess(org);
  return SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter((item) => !item.need || access.can(item.need)),
  }));
}

function SideNav({ org }: { org: string }) {
  const sections = useSections(org);
  const nav = useRef<HTMLElement>(null);
  const { pathname } = useLocation();
  // In a short window the list scrolls; the open section is kept in sight.
  useEffect(() => {
    nav.current?.querySelector('[data-status="active"]')?.scrollIntoView?.({ block: 'nearest' });
  }, [pathname]);
  return (
    <nav
      ref={nav}
      aria-label="Settings"
      // A short window: the list scrolls inside itself, so the last section stays reachable.
      className="sticky top-[calc(var(--topbar-height)+24px)] -mt-3 hidden max-h-[calc(100dvh-var(--topbar-height)-48px)] w-52 shrink-0 self-start overflow-y-auto pb-6 lg:block"
    >
      {sections.map((section) => (
        <div key={section.title} className="grid">
          <p className="px-3 pt-3 pb-1 text-caption text-faint">{section.title}</p>
          <ul className="grid">
            {section.items.map((item) => (
              <li key={item.to}>
                <Link
                  to={item.to}
                  params={{ org }}
                  className={cn(
                    'focus-inset flex h-8 items-center truncate rounded-lg px-3 text-secondary text-muted',
                    'transition-colors duration-(--duration-fast) ease-standard hover:bg-hover hover:text-fg',
                    'data-[status=active]:bg-accent-soft data-[status=active]:font-semibold data-[status=active]:text-accent-text',
                  )}
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

export function SettingsLayout() {
  const { org } = useParams({ from: '/$org/settings' });
  const { pathname } = useLocation();
  const isIndex = pathname.replace(/\/$/, '').endsWith('/settings');
  // A page inside a page has its own way back, and two would stack on a phone.
  const nested = pathname.includes('/settings/brains/add');
  return (
    <>
      {isIndex || nested ? null : (
        <Link
          to="/$org/settings"
          params={{ org }}
          className="-ml-1 mb-4 inline-flex h-8 items-center gap-1 rounded-full pr-3 pl-1 text-secondary text-muted hover:bg-hover hover:text-fg lg:hidden"
        >
          <ChevronLeft className="size-4" />
          Settings
        </Link>
      )}
      <h1 className={cn('mb-6 text-page-title-compact md:text-page-title lg:mb-8', !isIndex && 'hidden lg:block')}>Settings</h1>
      <div className="flex gap-8">
        <SideNav org={org} />
        <div className="min-w-0 max-w-180 flex-1">
          <Outlet />
        </div>
      </div>
    </>
  );
}

/** A phone gets the list of sections. A desktop already has it, so it goes to General. */
export function SettingsIndex() {
  const { org } = useParams({ from: '/$org/settings/' });
  const navigate = useNavigate();
  const sections = useSections(org);
  useEffect(() => {
    if (window.matchMedia('(min-width: 1024px)').matches) {
      void navigate({ to: '/$org/settings/general', params: { org }, replace: true });
    }
  }, [navigate, org]);
  return (
    <div className="grid gap-6 lg:hidden">
      {sections.map((section) => (
        <section key={section.title} aria-labelledby={`settings-${section.title}`}>
          <h2 id={`settings-${section.title}`} className="mb-2 px-1 text-caption text-faint">
            {section.title}
          </h2>
          <Card className="overflow-hidden">
          <ul className="divide-y divide-line">
            {section.items.map((item) => (
              <li key={item.to}>
                <Link
                  to={item.to}
                  params={{ org }}
                  className="focus-inset flex h-13 items-center gap-3 pr-3 pl-4 text-body active:bg-pressed"
                >
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                  <ChevronRight aria-hidden className="size-4 shrink-0 text-faint" />
                </Link>
              </li>
            ))}
          </ul>
          </Card>
        </section>
      ))}
    </div>
  );
}
