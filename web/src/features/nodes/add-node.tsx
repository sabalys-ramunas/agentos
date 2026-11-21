import { Link } from '@tanstack/react-router';
import { Cloud, Network, Server } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useCreateJoinToken, useNodes, useSession } from '@/api/hooks';
import type { JoinToken } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { CodeBlock } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { slugify, timeUntil } from '@/lib/format';

interface FlowProps {
  org: string;
  /** Called once when the new node has connected. */
  onConnected?: (id: string) => void;
}

/** Shared by the Add node dialog and onboarding. */
export function useAddNode({ org, onConnected }: FlowProps) {
  const nodes = useNodes(org);
  const create = useCreateJoinToken(org);
  const [token, setToken] = useState<JoinToken | null>(null);
  const [connected, setConnected] = useState<string | null>(null);
  const before = useRef<Set<string> | null>(null);

  const start = (name?: string) => {
    before.current = new Set((nodes.data ?? []).map((node) => node.id));
    create.mutate({ name: name || undefined }, { onSuccess: setToken });
  };

  // The event stream patches the fleet into the cache, so this is live, not polled.
  useEffect(() => {
    if (!token || connected || !before.current) return;
    const fresh = nodes.data?.find((node) => !before.current?.has(node.id));
    if (fresh) {
      setConnected(fresh.id);
      onConnected?.(fresh.id);
    }
  }, [nodes.data, token, connected, onConnected]);

  const reset = () => {
    setToken(null);
    setConnected(null);
    before.current = null;
  };

  // The "before" snapshot needs the real fleet, or an existing node looks brand new.
  return { token, connected, start, creating: create.isPending, ready: nodes.isSuccess, reset };
}

export function InstallCommand({ token, connected }: { token: JoinToken; connected: string | null }) {
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <p className="text-secondary font-medium">Run this on the machine</p>
        <CodeBlock code={token.install_command ?? ''} label="Copy install command" />
      </div>
      {/* On its own, so it never lands in shell history or the process list (security review 10). */}
      <div className="grid gap-2">
        <p className="text-secondary font-medium">Paste this when the installer asks for it</p>
        {token.token ? <CodeBlock code={token.token} label="Copy join token" /> : null}
        <p className="text-secondary text-faint">One-time token. Expires in {timeUntil(token.expires_at)}.</p>
      </div>
      <div role="status" className="flex h-12 items-center gap-2 rounded-lg border border-line px-4 text-body">
        {connected ? (
          <>
            <StatusDot tone="ok" />
            <span>
              <span className="mono">{connected}</span> is connected
            </span>
          </>
        ) : (
          <>
            <StatusDot tone="info" pulse />
            <span className="text-muted">Waiting for the machine</span>
          </>
        )}
      </div>
    </div>
  );
}

/** A node is one computer that runs alone: there is no one to join it to, so the dialog says so. */
export function useRunsAlone(org: string): boolean {
  const session = useSession();
  return session.data?.orgs.find((item) => item.slug === org)?.plan === 'self_hosted';
}

/**
 * The real ways to add a machine when this app runs on a node. A node is one machine and shows only
 * itself, so each way says plainly where the new machine will show.
 */
export function AddWays({ className }: { className?: string }) {
  return (
    <ul className={cn('grid gap-5', className)}>
      <li className="grid gap-2">
        <p className="flex items-center gap-2 text-label">
          <Cloud aria-hidden="true" className="size-4 text-faint" />A disposable VM, rented for one task
        </p>
        <p className="text-secondary text-muted">
          Give this node a Hetzner Cloud token once. Then an agent can rent a VM for a task; each one asks you first, deletes itself when its time is up, and shows here
          with its time left and cost.
        </p>
        <CodeBlock code="openajar vm setup" label="Copy the VM setup command" />
      </li>
      <li className="grid gap-2">
        <p className="flex items-center gap-2 text-label">
          <Server aria-hidden="true" className="size-4 text-faint" />
          Your own server, as a node of its own
        </p>
        <p className="text-secondary text-muted">
          From your laptop: it installs openajar on the server (Ubuntu or Debian, root or passwordless sudo) and connects your agents to it. It gets its own panel; this
          page keeps showing this machine.
        </p>
        <CodeBlock code="openajar connect root@my-server --install" label="Copy the connect command" />
      </li>
      <li className="grid gap-2">
        <p className="flex items-center gap-2 text-label">
          <Network aria-hidden="true" className="size-4 text-faint" />
          Several nodes at one address
        </p>
        <p className="text-secondary text-muted">On the box that runs the controller, make a one-time join line, then run the line it prints on the new machine.</p>
        <CodeBlock code="openajar controller join-token --name office-box" label="Copy the join token command" />
      </li>
    </ul>
  );
}

export function AddNodeDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const flow = useAddNode({ org });
  const alone = useRunsAlone(org);
  const [name, setName] = useState('');
  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      flow.reset();
      setName('');
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="Add a machine"
      description={
        alone
          ? 'This node is one machine, and this page shows it. These are the ways to give your agents more, each one command.'
          : 'A machine you control. It dials out to openajar, so no ports need opening.'
      }
      footer={
        flow.connected ? (
          <>
            <Button onClick={() => close(false)}>Done</Button>
            <Button variant="primary" asChild>
              <Link to="/$org/nodes/$id" params={{ org, id: flow.connected }} onClick={() => close(false)}>
                Open the machine
              </Link>
            </Button>
          </>
        ) : flow.token || alone ? (
          <Button onClick={() => close(false)}>Close</Button>
        ) : (
          <>
            <Button onClick={() => close(false)}>Cancel</Button>
            <Button variant="primary" disabled={!flow.ready} loading={flow.creating} onClick={() => flow.start(slugify(name))}>
              Get install command
            </Button>
          </>
        )
      }
    >
      {flow.token ? (
        <InstallCommand token={flow.token} connected={flow.connected} />
      ) : alone ? (
        <AddWays />
      ) : (
        <Field label="Name" hint="Optional. Otherwise the machine names itself.">
          <Input autoFocus mono placeholder="mac-mini" value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" />
        </Field>
      )}
    </Dialog>
  );
}
