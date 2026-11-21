/**
 * API keys. Only a person makes or revokes one. A key is shown once, on creation, and only its first
 * letters after that. It can read and nothing else unless a person says otherwise, and it never goes
 * above the ceiling they chose.
 */
import { KeyRound, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { useCreateSiteKey, useRevokeSiteKey, useSiteKeys } from '@/api/hooks';
import type { SiteApiDetail, SiteApiEffect, SiteKey, SiteKeyCreated } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { CodeBlock } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { EmptyState, ErrorState, InlineError, Skeleton } from '@/components/ui/states';
import { mainCell, Table, Td, Th, Tr } from '@/components/ui/table';
import { Checkbox } from '@/components/ui/toggle';
import { Tooltip } from '@/components/ui/menu';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { date, timeAgo } from '@/lib/format';
import { EFFECT_WORD } from './words';
import { PLACES } from '@/lib/vocabulary';

const CEILINGS: { value: SiteApiEffect; label: string; hint: string }[] = [
  { value: 'read', label: 'Only read', hint: 'The default. It can look, and nothing else.' },
  { value: 'draft', label: 'Read and fill in', hint: 'It can also type into a box like a search.' },
  { value: 'commit', label: 'Read, fill in and act', hint: 'It can change things on the site. Ask me first still applies.' },
];

function Reach({ item, api }: { item: SiteKey; api: SiteApiDetail }) {
  const named = item.endpoints?.flatMap((key) => api.endpoints.filter((endpoint) => endpoint.key === key).map((endpoint) => endpoint.title)) ?? [];
  const others = item.apis.length - 1;
  return (
    <span className="block truncate text-secondary text-muted">
      {named.length ? named.join(', ') : 'Every endpoint here'}
      {others > 0 ? `, and ${others} other Site ${others === 1 ? 'API' : 'APIs'}` : ''}
    </span>
  );
}

function NewKey({ org, api, open, onClose }: { org: string; api: SiteApiDetail; open: boolean; onClose: () => void }) {
  const create = useCreateSiteKey(org);
  const revoke = useRevokeSiteKey(org);
  const [label, setLabel] = useState('');
  const [picked, setPicked] = useState<string[]>(api.endpoints.map((endpoint) => endpoint.key));
  const [ceiling, setCeiling] = useState<SiteApiEffect>('read');
  const [made, setMade] = useState<SiteKeyCreated | null>(null);
  const [askingToClose, setAskingToClose] = useState(false);
  const close = () => {
    onClose();
    setMade(null);
    setLabel('');
    setCeiling('read');
    setAskingToClose(false);
    create.reset();
    revoke.reset();
  };
  if (made) {
    // The key is shown once. Leaving the dialog any way (Escape, a click outside, the close button)
    // asks first, so a key nobody copied is not lost by accident.
    const leave = (next: boolean) => {
      if (!next) setAskingToClose(true);
    };
    if (askingToClose) {
      return (
        <Dialog
          open={open}
          onOpenChange={leave}
          title="Close without saving the key?"
          description="Once this closes, the key cannot be shown again. If you have not copied it, revoke it and make a new one."
          footer={
            <>
              <Button
                onClick={() => {
                  setAskingToClose(false);
                  revoke.reset();
                }}
              >
                Go back to the key
              </Button>
              <Button variant="danger" loading={revoke.isPending} onClick={() => revoke.mutate(made.id, { onSuccess: close })}>
                Revoke it and close
              </Button>
            </>
          }
        >
          {revoke.error ? (
            <InlineError>
              {describeError(revoke.error)}
            </InlineError>
          ) : null}
        </Dialog>
      );
    }
    return (
      <Dialog
        open={open}
        onOpenChange={leave}
        title={`${made.label} is ready`}
        description="Copy it now. This is the only time it is shown, and it cannot be shown again."
        footer={
          <Button variant="primary" onClick={close}>
            I have saved it
          </Button>
        }
      >
        <CodeBlock code={made.secret} label="Copy the key" />
        <p className="mt-2 text-secondary text-muted">If it is lost, revoke it and make a new one.</p>
      </Dialog>
    );
  }
  const ready = label.trim() && picked.length > 0;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close();
      }}
      title="New key"
      description={`For calling ${api.title} from a tool of yours.`}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!ready}
            onClick={() =>
              create.mutate(
                { label: label.trim(), apis: [api.key], endpoints: picked.length === api.endpoints.length ? null : picked, ceiling, rate_per_min: 60, expires_at: null },
                { onSuccess: setMade },
              )
            }
          >
            Make the key
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        <Field label="What is it for?" hint={`This name shows in ${PLACES.history.word} beside everything it does.`}>
          <Input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Stock sheet" autoComplete="off" />
        </Field>
        <fieldset className="grid gap-2">
          <legend className="mb-2 text-secondary font-medium">What it can reach</legend>
          {api.endpoints.map((endpoint) => (
            <label key={endpoint.key} className="flex items-center gap-2 text-secondary">
              <Checkbox
                checked={picked.includes(endpoint.key)}
                onCheckedChange={(on) => setPicked((now) => (on === true ? [...now, endpoint.key] : now.filter((key) => key !== endpoint.key)))}
              />
              {endpoint.title}
            </label>
          ))}
        </fieldset>
        <Field label="The most it may do" hint={CEILINGS.find((item) => item.value === ceiling)?.hint}>
          <Select value={ceiling} onValueChange={(value) => setCeiling(value as SiteApiEffect)} options={CEILINGS.map(({ value, label: word }) => ({ value, label: word }))} />
        </Field>
        {create.error ? (
          <InlineError>
            {describeError(create.error)}
          </InlineError>
        ) : null}
      </div>
    </Dialog>
  );
}

export function Keys({ org, api }: { org: string; api: SiteApiDetail }) {
  const keys = useSiteKeys(org);
  const revoke = useRevokeSiteKey(org);
  const [adding, setAdding] = useState(false);
  const [leaving, setLeaving] = useState<SiteKey | null>(null);
  const mine = keys.data?.filter((item) => item.apis.includes(api.key)) ?? [];
  return (
    <section aria-labelledby="site-keys" className="min-w-0">
      <div className="mb-4 flex items-center justify-between gap-4">
        <div>
          <h2 id="site-keys" className="text-section-title">
            API keys
          </h2>
          <p className="mt-1 text-secondary text-muted">Each key is shown once, and can only read unless you say otherwise.</p>
        </div>
        <Button icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setAdding(true)}>
          New key
        </Button>
      </div>
      <div className="overflow-hidden rounded-(--radius-card) border border-line">
        {keys.isError ? (
          <ErrorState error={keys.error} onRetry={() => void keys.refetch()} />
        ) : !keys.data ? (
          <Skeleton className="h-36" />
        ) : mine.length === 0 ? (
          <EmptyState icon={<KeyRound />} title="No keys yet" description="A key lets a tool of yours call this. Nothing outside can reach it without one." />
        ) : (
          <Table aria-label="Keys for this Site API">
            <thead>
              <tr>
                <Th>Name</Th>
                <Th className="hidden md:table-cell">The most it may do</Th>
                <Th className="hidden text-right md:table-cell">Limit</Th>
                <Th className="text-right">Last used</Th>
                <Th aria-label="Revoke" />
              </tr>
            </thead>
            <tbody>
              {mine.map((item) => (
                <Tr key={item.id} className="hover:bg-hover">
                  <Td className={cn(mainCell)}>
                    <span className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium">{item.label}</span>
                      <span className="mono shrink-0 text-caption text-faint">{item.prefix}...</span>
                    </span>
                    <Reach item={item} api={api} />
                  </Td>
                  <Td className="hidden text-secondary md:table-cell">
                    {EFFECT_WORD[item.ceiling].word}
                    {item.expires_at ? <span className="block text-caption text-faint">Until {date(item.expires_at)}</span> : null}
                  </Td>
                  <Td className="tabular hidden text-right text-secondary text-muted md:table-cell">{item.rate_per_min} a minute</Td>
                  <Td className="tabular text-right text-secondary text-muted">{timeAgo(item.last_used_at)}</Td>
                  <Td className="w-12">
                    <Tooltip label={`Revoke ${item.label}`}>
                      <IconButton label={`Revoke ${item.label}`} size="sm" onClick={() => setLeaving(item)}>
                        <Trash2 aria-hidden="true" className="size-4" />
                      </IconButton>
                    </Tooltip>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </div>
      <NewKey org={org} api={api} open={adding} onClose={() => setAdding(false)} />
      <ConfirmDialog
        open={leaving !== null}
        onOpenChange={(open) => {
          if (!open) setLeaving(null);
        }}
        title={`Revoke ${leaving?.label ?? 'this key'}?`}
        description="Anything using it stops working at once. This cannot be undone, and a new key will be a new secret."
        confirmLabel="Revoke the key"
        danger
        loading={revoke.isPending}
        onConfirm={() => {
          if (leaving) revoke.mutate(leaving.id, { onSuccess: () => setLeaving(null) });
        }}
      />
    </section>
  );
}
