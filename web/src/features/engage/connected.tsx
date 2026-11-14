import { type FormEvent, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { useAddSurface, useConnectors, useEngageProgram, useRemoveSurface, useSurface, useWorkspaces } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { NewSurface } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { Input, Field } from '@/components/ui/field';
import { Select, type SelectOption } from '@/components/ui/select';
import { Status } from '@/components/ui/status';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { healthStatus } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { EFFECT_WORDS, ENGAGEMENT, PLACES } from '@/lib/vocabulary';
import { accountTitleOf } from './words';

/** The sites an engagement plan runs on today. Each is connected by the same two facts: a name and, on Instagram, an id. */
const SITES: SelectOption[] = [
  { value: 'instagram', label: 'Instagram' },
  { value: 'reddit', label: 'Reddit' },
];

/** What this account is connected to, and what its connections can do with it. Only the owner sees and changes them. */
export function ConnectedTab({ org, ws, name }: { org: string; ws: string; name: string }) {
  const id = `${ws}/${name}`;
  const surface = useSurface(org, ws, name);
  const connectors = useConnectors(org);
  const program = useEngageProgram(org, ws, name);
  const workspaces = useWorkspaces(org).data;
  const [technical] = useTechnical();
  const [connecting, setConnecting] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const remove = useRemoveSurface(org);

  const denied = [surface.error, connectors.error].some((error) => error instanceof ApiError && error.status === 403);
  if (denied) {
    return (
      <Card className="px-6 py-10 text-center">
        <p className="text-14 text-muted">The connections of an account are the owner’s to see.</p>
      </Card>
    );
  }
  // A 404 is the node's way of saying nothing is connected to this account yet, which is an answer, not a fault.
  const notConnected = surface.error instanceof ApiError && surface.error.status === 404;
  if (surface.isPending || connectors.isPending) return <Skeleton className="h-48 rounded-xl" />;
  if (surface.isError && !notConnected) {
    return (
      <Card>
        <ErrorState error={surface.error} onRetry={() => void surface.refetch()} />
      </Card>
    );
  }

  const current = notConnected ? null : surface.data ?? null;
  const operations = (connectors.data ?? []).filter((op) => op.surfaces.some((s) => s.surface === id));
  const who = current ? (current.acts_as.name ?? current.acts_as.user_id ?? current.acts_as.id ?? 'this account') : null;

  return (
    <div className="grid gap-10">
      <Section title={ENGAGEMENT.connected.word}>
        {current ? (
          <Card className="grid gap-3 px-4 py-4">
            <p className="text-14">
              Acts as <span className="font-medium">{who}</span>
            </p>
            <p className="text-13 text-muted">
              Through{' '}
              <Link to="/$org/lanes/$ws/$name" params={{ org, ws: current.lane.split('/')[0] ?? ws, name: current.lane.split('/')[1] ?? name }} className="text-blue hover:underline">
                {accountTitleOf(current.lane, workspaces)}
              </Link>
              {technical ? <span className="mono ml-2 text-12 text-faint">{current.id} · {current.site}</span> : null}
            </p>
            <div>
              <Button variant="danger" size="sm" onClick={() => setLeaving(true)}>
                {ENGAGEMENT.disconnect.word}
              </Button>
            </div>
          </Card>
        ) : (
          <EmptyState
            title="Not connected as an identity yet"
            description={`No ${PLACES.accounts.singular.toLowerCase()} acts as this one yet, so nothing can be done through it.`}
            action={
              <Button variant="primary" onClick={() => setConnecting(true)}>
                {ENGAGEMENT.connect.word}
              </Button>
            }
            className="py-10"
          />
        )}
      </Section>

      <Section title={ENGAGEMENT.canDo.word} description="What the connections can do here, and whether each one is ready.">
        {operations.length ? (
          <Card className="overflow-x-auto">
            <Table>
              <thead>
                <Tr>
                  <Th className={mainCell}>Action</Th>
                  <Th>Status</Th>
                  <Th className="hidden md:table-cell">Goes out</Th>
                </Tr>
              </thead>
              <tbody>
                {operations.map((op) => {
                  const here = op.surfaces.find((s) => s.surface === id);
                  return (
                    <Tr key={op.operation}>
                      <Td className={mainCell}>
                        <span className="block text-14">{op.title}</span>
                        {technical ? <span className="mono block text-12 text-faint">{op.operation} · {op.tool}</span> : null}
                      </Td>
                      <Td>
                        <Status status={healthStatus[here?.health ?? 'unknown']} />
                        {here?.health === 'held' && here.health_detail?.code ? (
                          <span className="mt-1 block text-12 text-muted">{String(here.health_detail.code).replace(/_/g, ' ')}</span>
                        ) : null}
                      </Td>
                      <Td className="hidden md:table-cell">
                        <span className="text-14">{EFFECT_WORDS[op.effect].word}</span>
                      </Td>
                    </Tr>
                  );
                })}
              </tbody>
            </Table>
          </Card>
        ) : (
          <EmptyState
            title="Nothing can act here yet"
            description="No installed connector runs on this account. Connectors come with the sites an account uses."
            className="py-10"
          />
        )}
      </Section>

      {connecting ? (
        <ConnectDialog
          org={org}
          ws={ws}
          name={name}
          defaultSite={program.data?.site === 'reddit' ? 'reddit' : 'instagram'}
          onClose={() => setConnecting(false)}
        />
      ) : null}
      <ConfirmDialog
        open={leaving}
        onOpenChange={(open) => {
          if (!open) setLeaving(false);
        }}
        title="Disconnect this account?"
        description={`${who ?? 'This identity'} stops being who this account acts as. Nothing posts as it until you connect it again.`}
        confirmLabel={ENGAGEMENT.disconnect.word}
        loading={remove.isPending}
        danger
        onConfirm={() => remove.mutate({ ws, name }, { onSuccess: () => setLeaving(false) })}
      >
        {remove.error instanceof ApiError ? <p role="alert" className="text-13 text-red">{remove.error.message}</p> : null}
      </ConfirmDialog>
    </div>
  );
}

/** Who the account acts as on a site: a name the site shows, and on Instagram the id it gives the business. */
function ConnectDialog({ org, ws, name, defaultSite, onClose }: { org: string; ws: string; name: string; defaultSite: string; onClose: () => void }) {
  const add = useAddSurface(org);
  const [site, setSite] = useState(defaultSite);
  const [who, setWho] = useState('');
  const [accountId, setAccountId] = useState('');
  const [tried, setTried] = useState(false);
  const instagram = site === 'instagram';
  const whoProblem = who.trim() === '' ? 'Say the name the site shows, such as @biza.coffee or u/biz_b_coffee.' : null;
  const idProblem = instagram && !/^\d+$/.test(accountId.trim()) ? 'Use the number the site gives the business account.' : null;
  const error = add.error instanceof ApiError ? add.error.message : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (whoProblem || idProblem) return;
    const body: NewSurface = {
      id: `${ws}/${name}`,
      site,
      lane: `${ws}/${name}`,
      acts_as: instagram
        ? { kind: 'ig_business', id: accountId.trim(), name: who.trim() }
        : { kind: 'account', user_id: who.trim().replace(/^u\//, ''), name: who.trim() },
    };
    add.mutate(body, { onSuccess: onClose });
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={ENGAGEMENT.connect.word}
      description="Who this account acts as on the site. Only you can connect one, in person."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" form="connect-identity" variant="primary" loading={add.isPending}>
            {ENGAGEMENT.connect.word}
          </Button>
        </>
      }
    >
      <form id="connect-identity" className="grid gap-5" onSubmit={submit} noValidate>
        <Field label="Site">
          <Select value={site} onValueChange={setSite} options={SITES} />
        </Field>
        <Field label="Name on the site" hint="As the site shows it" error={tried ? whoProblem : null}>
          <Input value={who} onChange={(event) => setWho(event.target.value)} placeholder={instagram ? '@your.business' : 'u/your-username'} autoComplete="off" />
        </Field>
        {instagram ? (
          <Field label="Account id" hint="The number the site gives the business account" error={tried ? idProblem : null}>
            <Input value={accountId} onChange={(event) => setAccountId(event.target.value)} inputMode="numeric" mono autoComplete="off" />
          </Field>
        ) : null}
        {error ? <p role="alert" className="text-13 text-red">{error}</p> : null}
      </form>
    </Dialog>
  );
}
