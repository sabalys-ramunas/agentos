/**
 * The devices a member is signed in on, for an admin to see and to end. Ending one signs that person
 * out there at once; they can sign in again. Their passkeys stay, and only they remove those.
 */
import { useState } from 'react';
import { useEndMemberDevice, useMember } from '@/api/hooks';
import type { Member, MemberSession } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { plural, timeAgo } from '@/lib/format';
import { toast } from 'sonner';

export function MemberDevices({ org, member, onClose }: { org: string; member: Member; onClose: () => void }) {
  const detail = useMember(org, member.id);
  const end = useEndMemberDevice(org);
  const [ending, setEnding] = useState<MemberSession | null>(null);
  const first = member.user.name.split(' ')[0] || member.user.name;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={`${member.user.name}'s devices`}
      description="Where this person is signed in right now. Ending one signs them out there at once."
      footer={
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      }
    >
      <QueryView
        query={detail}
        card={false}
        skeleton={
          <div role="status" aria-label="Loading" className="grid gap-3">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        }
      >
        {(data) => (
          <div className="grid gap-4">
            {data.devices.sessions.length ? (
              <ul className="divide-y divide-line border-y border-line">
                {data.devices.sessions.map((session) => (
                  <li key={session.id} className="flex items-center justify-between gap-4 py-3">
                    <div className="min-w-0">
                      <p className="truncate text-body">{session.label}</p>
                      <p className="text-secondary text-faint">
                        Signed in {timeAgo(session.created_at)} · last seen {timeAgo(session.last_seen_at)}
                      </p>
                    </div>
                    <Button size="sm" variant="danger" onClick={() => setEnding(session)} className="shrink-0">
                      End
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState title="Not signed in anywhere right now" description={`When ${first} signs in on a device, it shows here, and you can end it.`} />
            )}
            <p className="text-secondary text-muted">
              {plural(data.devices.passkeys.length, 'passkey')} set up for {first}. Only {first} can remove a passkey.
            </p>
          </div>
        )}
      </QueryView>
      <ConfirmDialog
        open={ending !== null}
        onOpenChange={(open) => {
          if (!open) setEnding(null);
        }}
        title="End this device?"
        description={`${first} is signed out there at once. They can sign in again on that device.`}
        confirmLabel="End device"
        danger
        loading={end.isPending}
        onConfirm={() =>
          ending &&
          end.mutate(
            { member: member.id, device: ending.id },
            {
              onSuccess: () => {
                setEnding(null);
                toast('That device is signed out');
              },
              onError: () => setEnding(null),
            },
          )
        }
      />
    </Dialog>
  );
}
