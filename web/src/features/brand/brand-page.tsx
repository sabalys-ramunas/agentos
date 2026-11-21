/**
 * Brand: the product and business profiles the owner has. A change an agent proposed leads the page;
 * each profile opens on its Components. Only the owner reads brand; anyone else gets a calm page.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowRight, Palette } from 'lucide-react';
import { useBrandProfiles, useBrandProposals, useWorkspaces } from '@/api/hooks';
import type { BrandProfileRow, BrandProposal } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Badge, Chip } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, cardLink } from '@/components/ui/card';
import { CodeBlock, PageHeader } from '@/components/ui/misc';
import { KindEyebrow, KindTile } from '@/components/ui/kind';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { plural, timeAgo } from '@/lib/format';
import { principalName } from '@/lib/principal';
import { PLACES } from '@/lib/vocabulary';
import { useTechnical } from '@/lib/technical';
import { brandColours } from '@/features/needs-you/if-yes';
import { BrandNotYours, isRefused } from './not-yours';
import type { BrandSearch } from './search';
import { cn } from '@/lib/cn';

const LEDE = "Who your work is for: each product's components, colours, type and guidelines, which agents design with and checks measure against.";

/** A colour as data: the brand's own value on a small tile with a hairline, and its hex beside it. */
function Colour({ value }: { value: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span data-studio="ignore" aria-hidden="true" className="size-8 rounded-md border border-line-strong" style={{ backgroundColor: value }} />
      <span className="font-mono text-code-caption text-muted">{value}</span>
    </span>
  );
}

/** A change an agent proposed: who, why in their words, what changes (a colour drawn before and after), and Review. */
function ProposalRow({ org, proposal }: { org: string; proposal: BrandProposal }) {
  const colours = brandColours(proposal.diff).slice(0, 2);
  return (
    <Card className="grid gap-4 border-accent-line p-5 md:grid-cols-[minmax(0,1fr)_auto] md:items-end md:p-6">
      <div className="grid min-w-0 gap-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <KindEyebrow kind="design" label="Brand change" />
          <Status chip status={{ label: 'Waiting for you', tone: 'live' }} />
          <span className="text-caption text-faint md:ml-auto">{timeAgo(proposal.created_at)}</span>
        </div>
        <h2 className="flex min-w-0 flex-wrap items-center gap-2 text-section-title">
          {proposal.created_by ? <AgentMark principal={proposal.created_by} size={20} /> : null}
          {proposal.created_by ? principalName(proposal.created_by) : 'An agent'} proposes version {proposal.version} of {proposal.display_name}
        </h2>
        <p className="text-body text-muted">
          {proposal.reason ? <span title={proposal.reason} className="line-clamp-2 inline">“{proposal.reason}” · </span> : null}
          {plural(proposal.diff.length, 'change')}
        </p>
        {colours.length ? (
          <ul aria-label="Colours that change" className="flex flex-wrap gap-x-8 gap-y-3">
            {colours.map((colour) => (
              <li key={colour.name} className="grid gap-1">
                <span className="text-caption text-faint">{colour.name}</span>
                <span className="flex items-center gap-3">
                  <Colour value={colour.from} />
                  <ArrowRight aria-label="becomes" className="size-4 text-faint" />
                  <Colour value={colour.to} />
                </span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <Button asChild variant="primary" icon={<ArrowRight className="size-4" />} className="justify-self-start md:justify-self-end">
        <Link to="/$org/brand/$profile" params={{ org, profile: proposal.ref }}>
          Review version {proposal.version}
        </Link>
      </Button>
    </Card>
  );
}

function ProfileCard({ org, row }: { org: string; row: BrandProfileRow }) {
  return (
    <Link
      to="/$org/brand/$profile"
      params={{ org, profile: row.ref }}
      className={cn(cardLink, 'group grid min-w-0 content-start gap-3 p-5')}
    >
      <span className="flex items-center gap-3">
        <KindTile kind="brand" size="lg" />
        <span className="grid min-w-0">
          <span className="truncate text-card-title text-fg">{row.display_name}</span>
          <span className="truncate font-mono text-code-caption text-faint">{row.ref}</span>
        </span>
        <ArrowRight aria-hidden="true" className="ml-auto size-4 shrink-0 text-faint transition-transform duration-(--duration-fast) group-hover:translate-x-0.5 group-hover:text-fg" />
      </span>
      {row.headline ? (
        <span title={row.headline} className="line-clamp-2 text-body text-muted">
          {row.headline}
        </span>
      ) : null}
      <span className="flex flex-wrap items-center gap-2">
        <Badge>{row.kind === 'product' ? 'Product' : 'Business'}</Badge>
        <span className="text-secondary text-faint">
          Version {row.version}
          {row.state !== 'approved' ? `, ${row.state}` : ''}
        </span>
        {row.draft_waiting ? <Status chip className="ml-auto" status={{ label: `Version ${row.draft_waiting} waiting`, tone: 'live' }} /> : null}
      </span>
    </Link>
  );
}

export function BrandPage() {
  const { org } = useParams({ from: '/$org' });
  const search = useSearch({ from: '/$org/brand' });
  const navigate = useNavigate({ from: '/$org/brand' });
  const profiles = useBrandProfiles(org);
  const allWorkspaces = useWorkspaces(org).data ?? [];
  const [technical] = useTechnical();
  const proposals = useBrandProposals(org, !isRefused(profiles.error));
  const set = (next: Partial<BrandSearch>) => void navigate({ search: (prev) => ({ ...prev, ...next }), replace: true });

  const header = <PageHeader title={PLACES.brand.word} description={LEDE} />;
  if (profiles.isPending) {
    return (
      <>
        {header}
        <div role="status" aria-label="Loading brand" className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }, (_, index) => (
            <Skeleton key={index} className="h-36 rounded-xl" />
          ))}
        </div>
      </>
    );
  }
  if (profiles.isError) {
    return (
      <>
        {header}
        <Card>{isRefused(profiles.error) ? <BrandNotYours /> : <ErrorState error={profiles.error} onRetry={() => void profiles.refetch()} />}</Card>
      </>
    );
  }
  const rows = profiles.data.profiles;
  const workspaces = [...new Set(rows.map((row) => row.workspace))].sort();
  const names = new Map(allWorkspaces.map((item) => [item.id, item.name]));
  const shown = rows.filter((row) => (!search.workspace || row.workspace === search.workspace) && (!search.waiting || row.draft_waiting));
  const waiting = (proposals.data ?? []).filter((item) => !search.workspace || item.workspace === search.workspace);

  if (rows.length === 0) {
    return (
      <>
        {header}
        <Card>
          <EmptyState
            icon={<Palette />}
            title={profiles.data.available ? 'No product profiles yet' : 'Profiles are not on this computer'}
            description="An agent can draft a product's brand from its running site: it opens a few pages in light and dark and reads the colours, type and spacing. You approve the draft here."
            action={
              <div className="w-[min(28rem,80vw)] text-left">
                {/* What to ask the agent, in words; the tool call itself only under Technical details. */}
                <CodeBlock
                  code={technical ? 'profile_learn_brand profile=acme/shop url=https://shop.example' : 'Learn the brand of my shop from https://shop.example'}
                  label="Copy for your agent"
                />
              </div>
            }
          />
        </Card>
      </>
    );
  }

  return (
    <>
      {header}
      {waiting.length ? (
        <section aria-label="Waiting for you" className="mb-8 grid gap-3">
          {waiting.map((proposal) => (
            <ProposalRow key={proposal.id} org={org} proposal={proposal} />
          ))}
        </section>
      ) : null}
      {workspaces.length > 1 || rows.some((row) => row.draft_waiting) ? (
        <div role="group" aria-label="Filter profiles" className="scrollbar-none -mx-4 mb-4 flex gap-2 overflow-x-auto px-4 sm:mx-0 sm:px-0">
          <Chip pressed={!search.workspace && !search.waiting} label="All" count={rows.length} onClick={() => set({ workspace: undefined, waiting: undefined })} />
          {workspaces.length > 1
            ? workspaces.map((workspace) => (
                <Chip
                  key={workspace}
                  pressed={search.workspace === workspace}
                  label={names.get(workspace) ?? workspace}
                  count={rows.filter((row) => row.workspace === workspace).length}
                  onClick={() => set({ workspace: search.workspace === workspace ? undefined : workspace })}
                />
              ))
            : null}
          <Chip pressed={!!search.waiting} label="Waiting for you" count={rows.filter((row) => row.draft_waiting).length} onClick={() => set({ waiting: search.waiting ? undefined : true })} />
        </div>
      ) : null}
      {shown.length ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {shown.map((row) => (
            <ProfileCard key={row.ref} org={org} row={row} />
          ))}
        </div>
      ) : (
        <Card>
          <EmptyState title="No profile matches" description="Nothing here fits these filters." />
        </Card>
      )}
    </>
  );
}
