/**
 * The sites this account may reach. Everyone who can see the account reads the list; only the owner adds or
 * removes one, and the node holds the change at once, even on an account that is already open.
 */
import { useState } from 'react';
import { useAccess, useLaneSites, useSetLaneSites } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Input } from '@/components/ui/field';
import { X } from 'lucide-react';
import { describeError } from '@/lib/errors';
import { AccountSection } from './account-section';
import { siteWords, sitesSentence, validSite } from './account-words';

export function SitesSection({ org, lane }: { org: string; lane: Lane }) {
  const sites = useLaneSites(org, lane.workspace, lane.name).data;
  const canChange = useAccess(org).can('lanes.sites');
  const change = useSetLaneSites(org, lane.workspace, lane.name);
  const [typed, setTyped] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  if (!sites || (!sites.sites.length && !canChange)) return null;

  const add = () => {
    const site = typed.trim();
    if (!validSite(site)) {
      setFormError('Type a site as its address, such as instagram.com.');
      return;
    }
    setFormError(null);
    change.mutate({ op: 'add', sites: [site] }, { onSuccess: () => setTyped(''), onError: (error) => setFormError(describeError(error)) });
  };
  const remove = (site: string) => {
    setFormError(null);
    change.mutate({ op: 'remove', sites: [site] }, { onError: (error) => setFormError(describeError(error)) });
  };

  return (
    <AccountSection id="account-sites" title="Sites">
      <p className="text-13 text-muted">{sitesSentence(sites)}</p>
      {sites.sites.length ? (
        <ul className="grid divide-y divide-line">
          {sites.sites.map((site) => {
            const { host, note } = siteWords(site);
            return (
              <li key={site} className="flex min-h-10 items-center justify-between gap-3 text-13">
                <span className="mono min-w-0 truncate">
                  {host}
                  {note ? <span className="ml-2 font-sans text-faint">{note}</span> : null}
                </span>
                {canChange ? (
                  <IconButton label={`Remove ${host}`} size="sm" onClick={() => remove(site)} aria-disabled={change.isPending || undefined}>
                    <X className="size-4" />
                  </IconButton>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {canChange ? (
        <form
          className="grid gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            add();
          }}
        >
          <div className="flex gap-2">
            <Input
              mono
              aria-label="Add a site"
              placeholder="instagram.com"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              className="min-w-0 flex-1"
            />
            <Button type="submit" aria-disabled={change.isPending || undefined}>
              Add the site
            </Button>
          </div>
          {formError ? (
            <p role="alert" className="text-13 text-red">
              {formError}
            </p>
          ) : null}
        </form>
      ) : null}
    </AccountSection>
  );
}
