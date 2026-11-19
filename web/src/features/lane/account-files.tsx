/**
 * An account's files on this computer: its inbox (what agents and people put there, ready to use) and its
 * outbox (what its browser downloaded). A person sees them, downloads them, and the owner or an operator adds
 * and deletes them. A file is only stored and handed back as bytes: nothing here opens or renders one.
 * Shown only for a browser account, and only while it has files or a person can add one.
 */
import { Download, Trash2, Upload } from 'lucide-react';
import { useRef, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useAccountFiles, useDeleteAccountFile, useDownloadAccountFile, useUploadAccountFile } from '@/api/hooks';
import type { AccountFile, AccountFiles, FileBox, Lane } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Status } from '@/components/ui/status';
import { describeError } from '@/lib/errors';
import { timeAgo } from '@/lib/format';
import { fileBox, fileStatus } from '@/lib/labels';
import { saveFile } from '@/lib/save-file';
import { AccountSection, Answer } from './account-section';
import { InlineError } from '@/components/ui/states';

const BOXES: FileBox[] = ['inbox', 'outbox'];

export function AccountFilesSection({ org, lane }: { org: string; lane: Lane }) {
  const files = useAccountFiles(org, lane.workspace, lane.name);
  const canOperate = useAccess(org).can('lanes.operate');
  const data = files.data;
  if (lane.kind !== 'browser' || !data) return null;
  const count = data.inbox.length + data.outbox.length;
  if (count === 0 && !canOperate) return null;
  return (
    <AccountSection
      id="account-files"
      title="Files"
      action={canOperate ? <AddFile org={org} lane={lane} limit={data.limits} /> : null}
    >
      <Answer tone="neutral" word={count ? `${count} ${count === 1 ? 'file' : 'files'} on this account` : 'No files yet'}>
        {`Each file can be up to ${data.limits.put}. Nothing here opens a file: it is only kept, and handed back as it is.`}
      </Answer>
      {count ? (
        <Card className="overflow-hidden">
          {BOXES.map((box) => (
            <FileGroup key={box} box={box} rows={box === 'inbox' ? data.inbox : data.outbox} org={org} lane={lane} canOperate={canOperate} />
          ))}
        </Card>
      ) : null}
    </AccountSection>
  );
}

function FileGroup({ box, rows, org, lane, canOperate }: { box: FileBox; rows: AccountFile[]; org: string; lane: Lane; canOperate: boolean }) {
  if (rows.length === 0) return null;
  return (
    <div className="border-t border-line first:border-t-0">
      <h3 className="px-4 pt-3 text-secondary text-faint font-semibold">{fileBox[box]}</h3>
      <ul className="divide-y divide-line">
        {rows.map((file) => (
          <FileRow key={file.id} file={file} org={org} lane={lane} canOperate={canOperate} />
        ))}
      </ul>
    </div>
  );
}

function FileRow({ file, org, lane, canOperate }: { file: AccountFile; org: string; lane: Lane; canOperate: boolean }) {
  const save = useDownloadAccountFile(org, lane.workspace, lane.name);
  const remove = useDeleteAccountFile(org, lane.workspace, lane.name);
  const [deleting, setDeleting] = useState(false);
  const status = fileStatus[file.status];
  const failure = save.error ?? remove.error;
  return (
    <li className="grid gap-1 px-4 py-2">
      <div className="flex min-h-10 items-center gap-3">
        <span className="min-w-0 flex-1">
          <span className="block truncate text-label">{file.name}</span>
          <span className="block truncate text-secondary text-faint">
            {file.size_text}
            {file.kind ? ` · ${file.kind}` : ''} · {timeAgo(file.created_at)}
          </span>
        </span>
        {file.status !== 'complete' ? <Status status={status} /> : null}
        {file.status === 'complete' ? (
          <IconButton
            label={`Download ${file.name}`}
            aria-disabled={save.isPending || undefined}
            onClick={() => {
              if (!save.isPending) save.mutate(file.id, { onSuccess: (download) => saveFile(download, file.name) });
            }}
          >
            <Download className="size-4" />
          </IconButton>
        ) : null}
        {canOperate ? (
          <IconButton label={`Delete ${file.name}`} onClick={() => setDeleting(true)}>
            <Trash2 className="size-4" />
          </IconButton>
        ) : null}
      </div>
      {file.error ? <p className="text-secondary text-muted">{file.error}</p> : null}
      {failure ? (
        <InlineError>
          {describeError(failure)}
        </InlineError>
      ) : null}
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete ${file.name}?`}
        description={`It is removed from ${fileBox[file.box].toLowerCase()} on this account. Nothing else changes, and it cannot be brought back.`}
        confirmLabel="Delete it"
        danger
        loading={remove.isPending}
        onConfirm={() =>
          remove.mutate(file.id, {
            onSuccess: () => {
              setDeleting(false);
              toast(`Deleted ${file.name}.`);
            },
          })
        }
      />
    </li>
  );
}

function AddFile({ org, lane, limit }: { org: string; lane: Lane; limit: AccountFiles['limits'] }) {
  const input = useRef<HTMLInputElement>(null);
  const upload = useUploadAccountFile(org, lane.workspace, lane.name);
  const [tooLarge, setTooLarge] = useState<string | null>(null);
  const choose = (file: File | undefined) => {
    if (!file) return;
    if (file.size > limit.put_bytes) {
      setTooLarge(`${file.name} is larger than the ${limit.put} limit, so it was not added.`);
      return;
    }
    setTooLarge(null);
    upload.mutate({ file, fileName: file.name }, { onSuccess: () => toast(`Added ${file.name}.`) });
  };
  return (
    <div className="grid justify-items-end gap-1">
      <Button size="sm" icon={<Upload className="size-4" />} loading={upload.isPending} onClick={() => input.current?.click()}>
        Add a file
      </Button>
      <input
        ref={input}
        type="file"
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          choose(event.target.files?.[0]);
          event.target.value = '';
        }}
      />
      {tooLarge || upload.error ? (
        <InlineError className="max-w-xs text-right">
          {tooLarge ?? describeError(upload.error)}
        </InlineError>
      ) : null}
    </div>
  );
}
