/**
 * One product or business profile. A waiting proposal leads the page; then it opens on Components,
 * the screen a design system team opens first, and goes on to colour, type, spacing and corners,
 * guidelines, references and environments. Only the owner sees it; anyone else gets a calm page.
 */
import { useParams, useSearch } from '@tanstack/react-router';
import { Copy, Check, ExternalLink } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useBrandProfile } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { BrandDecision } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { PageHeader, useCopy } from '@/components/ui/misc';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { date, safeUrl } from '@/lib/format';
import { refOf } from './model';
import { BrandNotYours, isRefused } from './not-yours';
import { Decided, Proposal } from './proposal';
import { Colours, Components, Guidelines, References, TypeAndSpace } from './sections';

const NAV: { id: string; label: string }[] = [
  { id: 'components', label: 'Components' },
  { id: 'colour', label: 'Colour' },
  { id: 'type', label: 'Type' },
  { id: 'space', label: 'Spacing and corners' },
  { id: 'guidelines', label: 'Guidelines' },
  { id: 'references', label: 'References' },
  { id: 'environments', label: 'Environments' },
];

function CopyForAgent({ profileRef }: { profileRef: string }) {
  const { copied, copy } = useCopy();
  return (
    <Button icon={copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />} onClick={() => copy(`profile_use profile=${profileRef} task=design`)}>
      {copied ? 'Copied' : 'Copy for an agent'}
    </Button>
  );
}

export function ProfilePage() {
  const { org, profile: param } = useParams({ from: '/$org/brand/$profile' });
  const search = useSearch({ from: '/$org/brand/$profile' });
  const ref = refOf(param);
  const query = useBrandProfile(org, ref);
  const malformed = !ref.includes('/');
  const profile = query.data;
  const [decided, setDecided] = useState<BrandDecision | null>(null);
  usePageCrumb(profile?.display_name ?? ref, { mono: !profile });

  useEffect(() => {
    if (!profile || !search.section) return;
    document.getElementById(`section-${search.section}`)?.scrollIntoView({ block: 'start' });
  }, [profile, search.section]);

  if (malformed) {
    return (
      <>
        <PageHeader title={ref} />
        <Card>
          <EmptyState title="No such profile" description="A profile's address names its workspace and its name, like harbor/shop." />
        </Card>
      </>
    );
  }
  if (query.isPending) {
    return (
      <div role="status" aria-label="Loading the profile" className="grid gap-6">
        <div className="grid gap-3">
          <Skeleton className="h-3 w-40" />
          <Skeleton className="h-8 w-72 max-w-full" />
          <Skeleton className="h-4 w-96 max-w-full" />
        </div>
        <Skeleton className="h-48 rounded-xl" />
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-56 rounded-xl" />
          <Skeleton className="h-56 rounded-xl" />
        </div>
      </div>
    );
  }
  if (query.isError || !profile) {
    const notFound = query.error instanceof ApiError && query.error.status === 404 && !isNotServed(query.error);
    return (
      <>
        <PageHeader title={ref} />
        <Card>
          {isRefused(query.error) ? (
            <BrandNotYours />
          ) : notFound ? (
            <EmptyState title="No such profile" description="It may have been removed, or the address is wrong." />
          ) : (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          )}
        </Card>
      </>
    );
  }

  const website = safeUrl(profile.website);
  return (
    <>
      <PageHeader
        eyebrow={
          <span className="flex flex-wrap items-center gap-2 font-mono text-code-caption text-faint">
            {profile.ref}
            <Badge>{profile.kind === 'product' ? 'Product' : 'Business'}</Badge>
          </span>
        }
        title={profile.display_name}
        description={
          <>
            {profile.headline ? `${profile.headline}. ` : ''}
            Version {profile.version}
            {profile.decided_at ? `, approved on ${date(profile.decided_at)}` : profile.state === 'draft' ? ', a draft nobody has approved yet' : ''}.
            {website ? (
              <>
                {' '}
                <a href={website} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 underline decoration-line-strong underline-offset-2 hover:text-fg">
                  {profile.website}
                  <ExternalLink aria-hidden="true" className="size-3" />
                </a>
              </>
            ) : null}
          </>
        }
        actions={<CopyForAgent profileRef={profile.ref} />}
      />
      {decided ? <Decided version={decided.version} approved={decided.state === 'approved'} onDone={() => setDecided(null)} /> : null}
      {profile.draft ? <Proposal org={org} profile={profile} draft={profile.draft} onDecided={setDecided} /> : null}
      <nav aria-label="Sections of the profile" className="scrollbar-none sticky top-0 z-10 -mx-4 mb-6 flex gap-6 overflow-x-auto border-b border-line bg-page px-4 sm:mx-0 sm:px-0">
        {NAV.map((item) => (
          <a
            key={item.id}
            href={`#section-${item.id}`}
            className="focus-inset -mb-px flex h-10 shrink-0 items-center border-b-2 border-transparent text-14 font-medium whitespace-nowrap text-faint hover:text-fg"
            onClick={(event) => {
              const target = document.getElementById(`section-${item.id}`);
              if (target) {
                event.preventDefault();
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }
            }}
          >
            {item.label}
          </a>
        ))}
      </nav>
      <div className="grid gap-10">
        <Components org={org} profile={profile} picked={search.component} />
        <Colours profile={profile} />
        <TypeAndSpace profile={profile} />
        <Guidelines profile={profile} />
        <References profile={profile} />
      </div>
    </>
  );
}
