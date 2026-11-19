import { useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from '@tanstack/react-router';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useDeleteOrg, useOrg, useUpdateOrg } from '@/api/hooks';
import { keys } from '@/api/keys';
import { Button } from '@/components/ui/button';
import { FormCard } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/field';
import { CopyButton } from '@/components/ui/misc';
import { InlineError, NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { describeError } from '@/lib/errors';
import { orgSlugProblem } from '@/lib/org-slug';
import { useTechnical } from '@/lib/technical';
import { SettingsHeader } from './page';

function Name({ org, name, canEdit }: { org: string; name: string; canEdit: boolean }) {
  const update = useUpdateOrg(org);
  const [value, setValue] = useState(name);
  useEffect(() => setValue(name), [name]);
  return (
    <FormCard
      title="Name"
      description="Shown at the top of your screens and to people you invite."
      footnote="Up to 80 characters."
      action={
        canEdit ? (
          <Button
            variant="primary"
            size="sm"
            loading={update.isPending}
            disabled={!value.trim() || value === name}
            onClick={() => update.mutate({ name: value.trim() }, { onSuccess: () => toast('Saved') })}
          >
            Save
          </Button>
        ) : undefined
      }
    >
      <Input aria-label="Name" className="max-w-sm" maxLength={80} readOnly={!canEdit} value={value} onChange={(event) => setValue(event.target.value)} />
      {update.isError ? (
        <InlineError>
          {describeError(update.error)}
        </InlineError>
      ) : null}
    </FormCard>
  );
}

function Address({ org, slug, canEdit }: { org: string; slug: string; canEdit: boolean }) {
  const update = useUpdateOrg(org);
  const client = useQueryClient();
  const navigate = useNavigate();
  const [value, setValue] = useState(slug);
  const [blurred, setBlurred] = useState(false);
  const [confirming, setConfirming] = useState(false);
  useEffect(() => setValue(slug), [slug]);
  const changed = value !== slug;
  const problem = changed ? orgSlugProblem(value) : null;
  const error = (blurred ? problem : null) ?? (update.isError ? describeError(update.error) : null);
  const host = window.location.host;

  // Awaited rather than a mutate callback: this screen unmounts when the old address stops
  // resolving, and React Query drops callbacks from an unmounted component.
  const save = async () => {
    setConfirming(false);
    const updated = await update.mutateAsync({ slug: value }).catch(() => null);
    if (!updated) return;
    await client.invalidateQueries({ queryKey: keys.session });
    toast('Address changed');
    await navigate({ to: '/$org/settings/general', params: { org: updated.slug }, replace: true });
  };

  return (
    <>
      <FormCard
        title="Address"
        description="Every link you share from here starts with this."
        footnote="Changing it breaks links people have saved."
        action={
          canEdit ? (
            <Button variant="primary" size="sm" disabled={!changed || Boolean(problem)} onClick={() => setConfirming(true)}>
              Save
            </Button>
          ) : undefined
        }
      >
        <div className="grid max-w-sm gap-2">
          <div className="flex h-8 items-center overflow-hidden rounded-md border border-line text-body focus-within:outline-2 focus-within:-outline-offset-1 focus-within:outline-(--focus)">
            <span className="flex h-full shrink-0 items-center border-r border-line bg-inset px-3 text-faint">{host}/</span>
            <input
              aria-label="Address"
              aria-invalid={error ? true : undefined}
              className="mono h-full min-w-0 flex-1 bg-transparent px-3 outline-none read-only:text-muted"
              readOnly={!canEdit}
              value={value}
              onChange={(event) => {
                setValue(event.target.value.toLowerCase());
                setBlurred(false);
                update.reset();
              }}
              onBlur={() => setBlurred(true)}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          {error ? (
            <InlineError>
              {error}
            </InlineError>
          ) : null}
        </div>
      </FormCard>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Change the address?"
        description={
          <>
            Links to <span className="mono">{host}/{slug}</span> stop working, and the address is free for anyone to take. Invites already sent
            keep working.
          </>
        }
        confirmLabel="Change address"
        loading={update.isPending}
        onConfirm={() => void save()}
      />
    </>
  );
}

export function GeneralSettings() {
  const { org } = useParams({ from: '/$org/settings/general' });
  const query = useOrg(org);
  const canEdit = useAccess(org).can('org.update');
  const [technical] = useTechnical();
  return (
    <>
      <SettingsHeader title="General" />
      <QueryView query={query} card skeleton={<Skeleton className="h-48 rounded-xl" />}>
        {(data) => (
          <div className="grid gap-6">
            <Name org={org} name={data.name} canEdit={canEdit} />
            <Address org={org} slug={data.slug} canEdit={canEdit} />
            {/* The id is for support, so it shows with Technical details, and not as a field of its own. */}
            {technical ? (
              <FormCard title="Id" description="Quote this when you contact support.">
                <div className="flex items-center gap-2">
                  <code className="mono min-w-0 truncate text-secondary text-muted">{data.id}</code>
                  <CopyButton text={data.id} label="Copy the id" />
                </div>
              </FormCard>
            ) : null}
          </div>
        )}
      </QueryView>
    </>
  );
}

export function DangerSettings() {
  const { org } = useParams({ from: '/$org/settings/danger' });
  const remove = useDeleteOrg(org);
  const navigate = useNavigate();
  const access = useAccess(org);
  const [open, setOpen] = useState(false);
  if (!access.can('org.delete')) return <NoAccess />;
  return (
    <>
      <SettingsHeader title="Danger zone" />
      <FormCard
        title="Delete this organization"
        description="Removes members, agent access, approvals and history from agentos. Your computers keep running, and their profiles and logins stay on your machines."
        footnote="This cannot be undone."
        action={
          <Button variant="danger" size="sm" onClick={() => setOpen(true)}>
            Delete organization
          </Button>
        }
      />
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Delete this organization?"
        description="Every member loses access at once, agent access stops working, and the address becomes free. Your computers keep running, and profiles and logins stay on your machines. This cannot be undone."
        confirmLabel="Delete organization"
        danger
        typeToConfirm={org}
        loading={remove.isPending}
        onConfirm={() =>
          remove.mutate(org, {
            onSuccess: () => {
              toast('Org deleted');
              void navigate({ to: '/' });
            },
          })
        }
      />
    </>
  );
}
