import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { RadioGroup } from 'radix-ui';
import { useState } from 'react';
import { toast } from 'sonner';
import { fleet } from '@/api/client';
import { ApiError } from '@/api/http';
import { keys } from '@/api/keys';
import type { LaneKind, Workspace } from '@/api/types';
import { LaneIcon } from '@/components/lane-icon';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input, Label } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { cn } from '@/lib/cn';
import { slugify } from '@/lib/format';

const KINDS: { value: LaneKind; label: string; hint: string; preset: string }[] = [
  { value: 'browser', label: 'Browser', hint: 'One logged-in account', preset: 'account' },
  { value: 'terminal', label: 'Terminal', hint: 'A shell on the computer', preset: 'workbench' },
  { value: 'service', label: 'Service', hint: 'A long-running process', preset: 'service' },
  { value: 'android', label: 'Phone', hint: 'An Android phone', preset: 'account' },
];

interface Props {
  org: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaces: Workspace[];
  defaultWorkspace: string;
}

export function NewLaneDialog({ org, open, onOpenChange, workspaces, defaultWorkspace }: Props) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [workspace, setWorkspace] = useState('');
  const [name, setName] = useState('');
  const [kind, setKind] = useState<LaneKind>('browser');
  const chosen = workspace || defaultWorkspace || workspaces[0]?.id || '';
  const slug = slugify(name);

  const create = useMutation({
    mutationFn: () =>
      fleet.createLane(org, {
        id: `${chosen}/${slug}`,
        kind,
        preset: KINDS.find((item) => item.value === kind)?.preset,
      }),
    meta: { inline: true },
    onSuccess: (lane) => {
      void client.invalidateQueries({ queryKey: keys.workspaces(org) });
      onOpenChange(false);
      setName('');
      toast('Account added');
      void navigate({ to: '/$org/lanes/$ws/$name', params: { org, ws: lane.workspace, name: lane.name } });
    },
  });

  const error = create.error instanceof ApiError ? create.error.message : null;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Add an account"
      description="An account is one isolated place for a website login or a task, pinned to a computer."
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" disabled={!slug || !chosen} loading={create.isPending} onClick={() => create.mutate()}>
            Create lane
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        <Field label="Workspace">
          <Select
            value={chosen}
            onValueChange={setWorkspace}
            options={workspaces.map((item) => ({ value: item.id, label: item.name }))}
          />
        </Field>
        <Field label="Name" hint={slug ? <span className="mono">{`${chosen}/${slug}`}</span> : 'For example, tiktok or staging-shell.'} error={error}>
          <Input autoFocus value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" />
        </Field>
        <div className="grid gap-2">
          <Label id="lane-kind">Kind</Label>
          <RadioGroup.Root
            aria-labelledby="lane-kind"
            value={kind}
            onValueChange={(value) => setKind(value as LaneKind)}
            className="grid grid-cols-2 gap-2"
          >
            {KINDS.map((item) => (
              <RadioGroup.Item
                key={item.value}
                value={item.value}
                className={cn(
                  'flex items-start gap-2 rounded-lg border border-line p-3 text-left transition-colors duration-(--duration-fast) ease-standard',
                  'hover:border-line-strong data-[state=checked]:border-fg',
                )}
              >
                <LaneIcon kind={item.value} className="mt-px" />
                <span className="min-w-0">
                  <span className="block text-label">{item.label}</span>
                  <span className="block text-caption text-faint">{item.hint}</span>
                </span>
              </RadioGroup.Item>
            ))}
          </RadioGroup.Root>
        </div>
      </div>
    </Dialog>
  );
}
