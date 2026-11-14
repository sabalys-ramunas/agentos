import { Snowflake } from 'lucide-react';
import { useAccess, useFreezeStatus, useNodes } from '@/api/hooks';
import { FreezeDialog, UnfreezeDialog } from '@/features/freeze/freeze';
import { computerName } from '@/lib/computers';
import { cn } from '@/lib/cn';
import { computerFreeze } from '@/lib/freeze';
import { Tooltip } from '../ui/menu';
import { openUnfreeze, setFreezeOpen, setUnfreezeOpen, useFreezeOpen, useUnfreezeOpen } from './palette-store';

/** The machine this app runs on, and what a person may do about a freeze on it. */
export function useFreezeTarget(org: string) {
  const access = useAccess(org);
  const freezes = useFreezeStatus(org);
  const nodes = useNodes(org).data ?? [];
  const frozen = computerFreeze(freezes.data);
  // The machine this app runs on is the one a freeze from here stops; a node without the service has none.
  const machine = freezes.data?.node ?? nodes[0]?.id ?? '';
  const freezable = freezes.data !== undefined && freezes.data.available !== false && machine !== '';
  return {
    machine,
    name: computerName(machine),
    frozen,
    canFreeze: access.can('freeze.computer') && freezable && !frozen,
    canLift: access.can('freeze.lift') && Boolean(frozen),
  };
}

/** The two dialogs, opened from the command bar or the top bar. Rendered once, by the shell. */
export function FreezeDialogs({ org }: { org: string }) {
  const target = useFreezeTarget(org);
  const freezing = useFreezeOpen();
  const lifting = useUnfreezeOpen();
  return (
    <>
      <FreezeDialog org={org} open={freezing} onOpenChange={setFreezeOpen} scope="node" name={target.name} />
      {target.frozen ? (
        <UnfreezeDialog org={org} open={lifting} onOpenChange={setUnfreezeOpen} scope={target.frozen.scope} target={target.frozen.target} name={target.name} />
      ) : null}
    </>
  );
}

/**
 * A freeze is the loudest state the app has, so while one holds the top bar says so on every page.
 * Whoever may lift it gets the Unfreeze dialog from here; anyone else reads it.
 */
export function FrozenChip({ org }: { org: string }) {
  const target = useFreezeTarget(org);
  if (!target.frozen) return null;
  const words = target.frozen.scope === 'mesh' ? 'Every machine is frozen' : `${target.name} is frozen`;
  const chip = cn(
    'inline-flex h-7 shrink-0 items-center gap-1 rounded-full border border-bad-line bg-bad-fill px-2 text-caption font-medium text-bad',
  );
  const label = (
    <>
      <Snowflake className="size-3" aria-hidden="true" />
      <span className="hidden sm:inline">Frozen</span>
    </>
  );
  const sentence = `${words}. No agent acts until the freeze is lifted.`;
  if (!target.canLift) {
    return (
      <span role="status" title={sentence} className={chip}>
        {label}
        <span className="sr-only">{sentence}</span>
      </span>
    );
  }
  return (
    <Tooltip label={sentence} side="bottom">
      <button type="button" className={cn(chip, 'hover:border-bad')} aria-label={`${words}. Lift the freeze`} onClick={openUnfreeze}>
        {label}
      </button>
    </Tooltip>
  );
}
