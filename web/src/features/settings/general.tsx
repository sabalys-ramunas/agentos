import { useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from '@tanstack/react-router';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useDeleteOrg, useOrg, useUpdateOrg } from '@/api/hooks';
import { keys } from '@/api/keys';
import { Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { FormCard } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { AffixInput, Field, Input } from '@/components/ui/field';
import { FormActions, FormNote, useSaved } from '@/components/ui/form';
import { UnsavedChanges } from '@/components/unsaved-changes';
import { CopyButton } from '@/components/ui/misc';
import { NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { AppTile } from '@/components/ui/tile';
import { describeError } from '@/lib/errors';
import { orgSlugProblem } from '@/lib/org-slug';
import { useTechnical } from '@/lib/technical';
import { SettingsHeader } from './page';

function Name({ org, name, canEdit, onDirty }: { org: string; name: string; canEdit: boolean; onDirty: (dirty: boolean) => void }) {
  const update = useUpdateOrg(org);
  const { saved, savedAt, flash, clear } = useSaved();
  const [value, setValue] = useState(name);
  const [tried, setTried] = useState(false);
  useEffect(() => setValue(name), [name]);
  const dirty = value.trim() !== name;
  useEffect(() => onDirty(dirty), [dirty, onDirty]);
  const problem = value.trim() ? null : 'Give the organization a name. People you invite see it.';
  const error = (tried ? problem : null) ?? (update.isError ? describeError(update.error) : null);
  const save = () => {
    setTried(true);
    if (problem) return;
    update.mutate({ name: value.trim() }, { onSuccess: flash });
  };
  return (
    <FormCard
      title="Name"
      description="Shown at the top of your screens and to people you invite."
      footnote={
        canEdit ? (
          <FormNote hint="Up to 80 characters." dirty={dirty} saving={update.isPending} savedAt={savedAt} problems={error ? 1 : 0} />
        ) : (
          'Only an owner or admin can change it.'
        )
      }
      onSubmit={canEdit ? save : undefined}
      action={
        canEdit ? (
          <FormActions
            dirty={dirty}
            saving={update.isPending}
            saved={saved}
            onDiscard={() => {
              setValue(name);
              setTried(false);
              update.reset();
            }}
          />
        ) : undefined
      }
    >
      <Field label="Name" hideLabel error={error} required className="max-w-105">
        <Input
          maxLength={80}
          readOnly={!canEdit}
          value={value}
          autoComplete="organization"
          onChange={(event) => {
            setValue(event.target.value);
            clear();
            update.reset();
          }}
          onBlur={() => setTried(true)}
        />
      </Field>
    </FormCard>
  );
}

function Address({ org, slug, canEdit, onDirty }: { org: string; slug: string; canEdit: boolean; onDirty: (dirty: boolean) => void }) {
  const update = useUpdateOrg(org);
  const client = useQueryClient();
  const navigate = useNavigate();
  const [value, setValue] = useState(slug);
  const [blurred, setBlurred] = useState(false);
  const [confirming, setConfirming] = useState(false);
  useEffect(() => setValue(slug), [slug]);
  const changed = value !== slug;
  useEffect(() => onDirty(changed), [changed, onDirty]);
  const problem = changed ? orgSlugProblem(value) : null;
  const error = (blurred ? problem : null) ?? (update.isError ? describeError(update.error) : null);
  const host = window.location.host;

  // Awaited rather than a mutate callback: this screen unmounts when the old address stops
  // resolving, and React Query drops callbacks from an unmounted component.
  const save = async () => {
    setConfirming(false);
    onDirty(false);
    const updated = await update.mutateAsync({ slug: value }).catch(() => null);
    if (!updated) {
      onDirty(true);
      return;
    }
    await client.invalidateQueries({ queryKey: keys.session });
    toast('Address changed');
    await navigate({ to: '/$org/settings/general', params: { org: updated.slug }, replace: true });
  };

  return (
    <>
      <FormCard
        title="Address"
        description="Every link you share from here starts with this."
        footnote={
          <FormNote hint="Changing it breaks links people have saved." dirty={changed} saving={update.isPending} problems={error ? 1 : 0} />
        }
        onSubmit={
          canEdit
            ? () => {
                setBlurred(true);
                if (changed && !problem) setConfirming(true);
              }
            : undefined
        }
        action={
          canEdit ? (
            <FormActions
              dirty={changed}
              saving={update.isPending}
              onDiscard={() => {
                setValue(slug);
                setBlurred(false);
                update.reset();
              }}
            />
          ) : undefined
        }
      >
        <Field label="Address" hideLabel error={error} required className="max-w-115">
          <AffixInput
            prefix={`${host}/`}
            mono
            readOnly={!canEdit}
            value={value}
            onChange={(event) => {
              setValue(event.target.value.toLowerCase());
              setBlurred(false);
              update.reset();
            }}
            onBlur={() => setBlurred(true)}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
          />
        </Field>
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
  const [nameDirty, setNameDirty] = useState(false);
  const [addressDirty, setAddressDirty] = useState(false);
  return (
    <>
      <SettingsHeader title="General" description="Your organization's name and address." />
      <UnsavedChanges when={nameDirty || addressDirty} what={nameDirty ? 'the name' : 'the address'} />
      <QueryView query={query} card skeleton={<Skeleton className="h-48 rounded-xl" />}>
        {(data) => (
          <div className="grid gap-4">
            <Name org={org} name={data.name} canEdit={canEdit} onDirty={setNameDirty} />
            <Address org={org} slug={data.slug} canEdit={canEdit} onDirty={setAddressDirty} />
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
  const name = useOrg(org).data?.name ?? org;
  const [open, setOpen] = useState(false);
  if (!access.can('org.delete')) return <NoAccess />;
  return (
    <>
      <SettingsHeader title="Danger zone" description="Things here cannot be undone." />
      <FormCard
        tone="danger"
        tile={<AppTile tone="red" icon={Trash2} size="sm" />}
        title="Delete this organization"
        description="Removes members, agent access, approvals and history from openajar. Your computers keep running, and their profiles and logins stay on your machines."
        action={
          <Button variant="danger" size="sm" onClick={() => setOpen(true)}>
            Delete organization
          </Button>
        }
      />
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Delete ${name} for good?`}
        description={
          <>
            Every member loses access at once, agent access stops working, and the address becomes free. Your computers keep running, and profiles and
            logins stay on your machines. <span className="font-semibold text-fg">This cannot be undone.</span>
          </>
        }
        confirmLabel="Delete for good"
        danger
        typeToConfirm={name}
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
