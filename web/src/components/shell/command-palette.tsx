import { BusinessMark } from '../brand/identity';
import { useNavigate } from '@tanstack/react-router';
import { Command, useCommandState } from 'cmdk';
import { Inbox, Keyboard, KeyRound, LogOut, Moon, Plus, ScanSearch, Search, Server, Settings, Snowflake, Sun, UserPlus } from 'lucide-react';
import { Dialog as Primitive } from 'radix-ui';
import type { ReactNode } from 'react';
import { useEffect, useState } from 'react';
import { useAccess, useNodes, useSession, useSignOut, useWorkspaces } from '@/api/hooks';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { isLaneInput } from '@/lib/keyboard';
import { businessSentence } from '@/features/home/words';
import { computerName } from '@/lib/computers';
import { laneName } from '@/lib/labels';
import { STATE_WORDS, laneCondition } from '@/lib/states';
import type { Condition } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { setTheme } from '@/lib/theme';
import { PLACES } from '@/lib/vocabulary';
import { SiteMark } from '../brand/site-mark';
import { useReturnFocus } from '../ui/dialog';
import { Kbd } from '../ui/misc';
import { commandSources, registerCommandSource } from './commands';
import type { CommandSource } from './commands';
import { useFreezeTarget } from './freeze-control';
import { menuFor } from './nav';
import { openFreeze, openShortcuts, openUnfreeze, setPaletteOpen, usePaletteOpen } from './palette-store';

const itemClass =
  'flex h-11 cursor-pointer items-center gap-3 rounded-lg px-3 text-body text-fg select-none data-[selected=true]:bg-hover [&_svg]:text-faint';

/** The letters typed, in violet, wherever they fall in a row's words (each word of the search on its own). */
function Highlight({ text, query }: { text: string; query: string }) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return <>{text}</>;
  const lower = text.toLowerCase();
  const marks: boolean[] = Array.from({ length: text.length }, () => false);
  for (const word of words) {
    let at = lower.indexOf(word);
    while (at >= 0) {
      for (let index = at; index < at + word.length; index += 1) marks[index] = true;
      at = lower.indexOf(word, at + word.length);
    }
  }
  const parts: ReactNode[] = [];
  let start = 0;
  for (let index = 1; index <= text.length; index += 1) {
    if (index === text.length || marks[index] !== marks[start]) {
      const piece = text.slice(start, index);
      parts.push(marks[start] ? <mark key={start} className="bg-transparent font-semibold text-accent-text">{piece}</mark> : piece);
      start = index;
    }
  }
  return <>{parts}</>;
}

/** The foot of the bar: how many rows match, and the keys. */
function Footer({ searching }: { searching: boolean }) {
  const count = useCommandState((state) => state.filtered.count);
  return (
    <div className="flex h-11 items-center gap-4 border-t border-line bg-inset px-4 text-caption text-faint">
      <span aria-live="polite" className="mr-auto tabular">
        {searching ? `${count} ${count === 1 ? 'result' : 'results'}` : 'Type to search everything'}
      </span>
      <span className="hidden items-center gap-1 sm:inline-flex">
        <Kbd>↑</Kbd>
        <Kbd>↓</Kbd>
        move
      </span>
      <span className="hidden items-center gap-1 sm:inline-flex">
        <Kbd>↵</Kbd>
        open
      </span>
      <span className="hidden items-center gap-1 sm:inline-flex">
        <Kbd>?</Kbd>
        all keys
      </span>
    </div>
  );
}

const wordOf = (condition: Condition) => condition.label ?? STATE_WORDS[condition.state];

/** With nothing typed, the bar offers the places most used; the rest appear as you type. */
const FIRST_PLACES = new Set(['h', 'v', 'n', 'r', 'c', 'm']);

function Item({ value, onSelect, children, hint }: { value: string; onSelect: () => void; children: ReactNode; hint?: ReactNode }) {
  return (
    <Command.Item value={value} onSelect={onSelect} className={itemClass}>
      {children}
      {hint ? <span className="ml-auto flex min-w-0 shrink-0 items-center gap-1 text-secondary text-faint">{hint}</span> : null}
    </Command.Item>
  );
}

/** Every word must appear. cmdk's default is fuzzy, so "insta" also matched "Juniper Labs". */
function matchWords(value: string, search: string) {
  const text = value.toLowerCase();
  return search.toLowerCase().split(/\s+/).every((word) => text.includes(word)) ? 1 : 0;
}

const groupClass =
  'mb-1 [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pt-3 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-secondary [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-faint';

/** Every machine, once the person types: the shell's own source, built the way a feature adds one. */
registerCommandSource({
  id: 'machines',
  heading: PLACES.machines.word,
  order: 50,
  onlyWhenSearching: true,
  useItems: (org) =>
    (useNodes(org).data ?? []).map((node) => ({
      id: node.id,
      label: computerName(node.id),
      detail: node.id,
      mono: true,
      icon: Server,
      keywords: 'machine computer node',
      onSelect: ({ navigate }) => void navigate({ to: '/$org/nodes/$id', params: { org, id: node.id } }),
    })),
});

/** Fixed for the life of the page, so each source's hook is called in the same order every time. */
const SOURCES = commandSources();

function SourceGroup({ source, org, query, run }: { source: CommandSource; org: string; query: string; run: (action: () => void) => void }) {
  const items = source.useItems(org, query);
  const navigate = useNavigate();
  // With nothing typed the bar is the places and a few actions; a feature's rows appear as you type.
  if (!items.length || !query.trim()) return null;
  return (
    <Command.Group heading={source.heading} className={groupClass}>
      {items.map((item) => (
        <Item
          key={item.id}
          value={`${source.heading} ${item.label} ${item.detail ?? ''} ${item.keywords ?? ''} ${source.id}:${item.id}`}
          onSelect={() => run(() => item.onSelect({ org, navigate }))}
          hint={
            item.keys ? (
              item.keys.map((key) => <Kbd key={key}>{key}</Kbd>)
            ) : item.detail ? (
              <span className={item.mono ? 'max-w-48 truncate font-mono text-code-caption' : 'max-w-48 truncate'}>{item.detail}</span>
            ) : undefined
          }
        >
          {item.icon ? <item.icon className="size-4 shrink-0" /> : null}
          <span className="min-w-0 truncate">
            <Highlight text={item.label} query={query} />
          </span>
        </Item>
      ))}
    </Command.Group>
  );
}

/**
 * The command bar (Cmd-K, or Search or run in the rail): every place with its "g" keys, then what
 * features add (runs, checks, machines: see commands.ts), the workspaces, accounts once you type,
 * and the actions.
 */
export function CommandPalette({ org }: { org: string }) {
  const open = usePaletteOpen();
  const [query, setQuery] = useState('');
  const navigate = useNavigate();
  const workspaces = useWorkspaces(org).data ?? [];
  const orgs = useSession().data?.orgs ?? [];
  const signOut = useSignOut();
  const [, setWorkspace] = useActiveWorkspace(org);
  const returnFocus = useReturnFocus(open);
  const access = useAccess(org);
  const freeze = useFreezeTarget(org);
  const me = useMe();

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // Ctrl-K is "delete to end of line" in a shell, so a lane keeps it.
      if (event.key === 'k' && (event.metaKey || event.ctrlKey) && !isLaneInput(event)) {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const run = (action: () => void) => {
    setPaletteOpen(false);
    setQuery('');
    action();
  };

  // The 29 accounts never appear as a flat list: they show up only once you search.
  const searching = query.trim().length > 0;
  const accountsGroup = (
                <Command.Group heading="Accounts" className={groupClass}>
                  {workspaces.flatMap((workspace) =>
                    workspace.lanes.map((lane) => (
                      <Item
                        key={lane.id}
                        value={`account lane ${lane.id} ${lane.site?.label ?? laneName(lane.name)} ${workspace.name}`}
                        hint={<span className="max-w-48 truncate">{wordOf(laneCondition(lane, { me }))}</span>}
                        onSelect={() =>
                          run(
                            () =>
                              void navigate({
                                to: '/$org/lanes/$ws/$name',
                                params: { org, ws: lane.workspace, name: lane.name },
                              }),
                          )
                        }
                      >
                        <SiteMark site={lane.site?.key ?? lane.name} label={false} />
                        <span className="min-w-0 truncate">
                          <Highlight text={`${workspace.name} · ${lane.site?.label ?? laneName(lane.name)}`} query={query} />
                        </span>
                      </Item>
                    )),
                  )}
                </Command.Group>
  );
  const dark = typeof document !== 'undefined' && (document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && window.matchMedia?.('(prefers-color-scheme: dark)').matches));

  return (
    <Primitive.Root
      open={open}
      onOpenChange={(next) => {
        setPaletteOpen(next);
        if (!next) setQuery('');
      }}
    >
      <Primitive.Portal>
        <Primitive.Overlay className="fixed inset-0 z-40 bg-overlay data-[state=closed]:animate-fade-out data-[state=open]:animate-fade-in" />
        <Primitive.Content
          onCloseAutoFocus={returnFocus}
          aria-label="Search or run a command"
          className="fixed top-3 left-1/2 z-50 w-[calc(100vw-16px)] max-w-160 -translate-x-1/2 overflow-hidden rounded-xl border border-line bg-raised shadow-dialog outline-none data-[state=closed]:animate-fade-out data-[state=open]:animate-fade-in sm:top-[min(16vh,110px)]"
        >
          <Primitive.Title className="sr-only">Search or run a command</Primitive.Title>
          <Primitive.Description className="sr-only">Go to a place, find a run, a check, a machine or an account, or run an action.</Primitive.Description>
          <Command loop filter={matchWords}>
            <div className="flex h-14 items-center gap-3 border-b border-line px-5">
              <Search className="size-4 shrink-0 text-faint" aria-hidden="true" />
              <Command.Input
                value={query}
                onValueChange={setQuery}
                placeholder="Go to, open a run, check a page, find a machine..."
                className="h-full min-w-0 flex-1 bg-transparent text-lede text-fg outline-none placeholder:text-faint"
              />
              <Kbd className="hidden sm:inline-flex">Esc</Kbd>
            </div>
            <Command.List className="max-h-[min(560px,64dvh)] overflow-y-auto p-2">
              <Command.Empty className="px-3 py-8 text-center text-body text-muted">
                Nothing called “{query.trim()}”. Try a page, an account or a run.
              </Command.Empty>
              {/* Searching, the accounts come first: they are what people look for most by name. */}
              {searching ? accountsGroup : null}
              <Command.Group heading="Go to" className={groupClass}>
                {menuFor(access.role).all.filter((item) => searching || FIRST_PLACES.has(item.key)).map((item) => (
                  <Item
                    key={item.to}
                    value={`go ${item.label}`}
                    onSelect={() => run(() => void navigate({ to: item.to, params: { org } }))}
                    hint={
                      <>
                        <Kbd>G</Kbd>
                        <Kbd>{item.key.toUpperCase()}</Kbd>
                      </>
                    }
                  >
                    <item.icon className="size-4" />
                    <Highlight text={item.label} query={query} />
                  </Item>
                ))}
              </Command.Group>
              {SOURCES.map((source) => (
                <SourceGroup key={source.id} source={source} org={org} query={query} run={run} />
              ))}
              {searching && workspaces.length ? (
                <Command.Group heading="Workspaces" className={groupClass}>
                  {workspaces.map((workspace) => (
                    <Item
                      key={workspace.id}
                      value={`workspace ${workspace.name} ${workspace.id}`}
                      hint={<span className="max-w-48 truncate">{businessSentence(workspace).text}</span>}
                      onSelect={() =>
                        run(() => {
                          setWorkspace(workspace.id);
                          void navigate({ to: '/$org', params: { org } });
                        })
                      }
                    >
                      <BusinessMark name={workspace.name} color={workspace.color} />
                      <span className="min-w-0 truncate">
                        <Highlight text={workspace.name} query={query} />
                      </span>
                    </Item>
                  ))}
                </Command.Group>
              ) : null}
              <Command.Group heading="Do" className={groupClass}>
                <Item value="action review needs you approvals" onSelect={() => run(() => void navigate({ to: '/$org/needs-you', params: { org } }))}>
                  <Inbox className="size-4" />
                  Review what needs you
                </Item>
                <Item value="action check a page design ui_check" onSelect={() => run(() => void navigate({ to: '/$org/checks', params: { org } }))}>
                  <ScanSearch className="size-4" />
                  Check a page
                </Item>
                {access.role !== 'approver' ? (
                  <Item value="action add machine computer node" onSelect={() => run(() => void navigate({ to: '/$org/nodes', params: { org }, search: { add: true } }))}>
                    <Plus className="size-4" />
                    Add a machine
                  </Item>
                ) : null}
                <Item value="action invite someone member" onSelect={() => run(() => void navigate({ to: '/$org/settings/invites', params: { org } }))}>
                  <UserPlus className="size-4" />
                  Invite someone
                </Item>
                <Item value="action theme dark light switch" onSelect={() => run(() => setTheme(dark ? 'light' : 'dark'))}>
                  {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
                  {dark ? 'Switch to light' : 'Switch to dark'}
                </Item>
                {/* Break glass stays one press away, typed or not. */}
                {freeze.canFreeze ? (
                  <Item value="action freeze stop everything machine computer agents" onSelect={() => run(openFreeze)}>
                    <Snowflake className="size-4" />
                    Freeze {freeze.name}
                  </Item>
                ) : null}
                {freeze.canLift ? (
                  <Item value="action unfreeze lift freeze machine computer agents" onSelect={() => run(openUnfreeze)}>
                    <Snowflake className="size-4" />
                    Unfreeze {freeze.name}
                  </Item>
                ) : null}
                {searching ? (
                  <>
                    <Item value="action agent access grant token" onSelect={() => run(() => void navigate({ to: '/$org/settings/grants', params: { org } }))}>
                      <KeyRound className="size-4" />
                      Give an AI app access
                    </Item>
                    <Item value="action keyboard shortcuts keys help" onSelect={() => run(openShortcuts)} hint={<Kbd>?</Kbd>}>
                      <Keyboard className="size-4" />
                      Keyboard shortcuts
                    </Item>
                    {orgs
                      .filter((item) => item.slug !== org)
                      .map((item) => (
                        <Item key={item.id} value={`action switch org ${item.name}`} onSelect={() => run(() => void navigate({ to: '/$org', params: { org: item.slug } }))}>
                          <Settings className="size-4" />
                          Switch to {item.name}
                        </Item>
                      ))}
                    <Item value="action sign out" onSelect={() => run(() => signOut.mutate(undefined, { onSuccess: () => void navigate({ to: '/sign-in' }) }))}>
                      <LogOut className="size-4" />
                      Sign out
                    </Item>
                  </>
                ) : null}
              </Command.Group>
            </Command.List>
            <Footer searching={searching} />
          </Command>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
