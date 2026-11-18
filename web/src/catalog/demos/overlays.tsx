/** Overlays: dialog, confirm, drawer, side panel, popover and tooltip. Each opens from a button. */
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog, Dialog, Drawer, SidePanel } from '@/components/ui/dialog';
import { IfYes } from '@/components/ui/if-yes';
import { Popover, PopoverContent, PopoverTrigger, Tooltip } from '@/components/ui/menu';

export function OverlaysDemo() {
  const [open, setOpen] = useState<'dialog' | 'confirm' | 'drawer' | null>(null);
  const [panel, setPanel] = useState(false);
  const close = () => setOpen(null);
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Card className="grid content-start gap-3 p-5">
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setOpen('dialog')}>Open a dialog</Button>
          <Button onClick={() => setOpen('confirm')}>Open a confirm</Button>
          <Button onClick={() => setOpen('drawer')}>Open a drawer</Button>
          <Button onClick={() => setPanel((value) => !value)} aria-expanded={panel}>
            {panel ? 'Close the side panel' : 'Open the side panel'}
          </Button>
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="ghost">Open a popover</Button>
            </PopoverTrigger>
            <PopoverContent className="w-64 p-4 text-secondary text-muted">A popover holds a few lines about one thing, and closes on Esc.</PopoverContent>
          </Popover>
          <Tooltip label="The full text of a cut line, or what an icon does">
            <Button variant="ghost">Hover for a tooltip</Button>
          </Tooltip>
        </div>
        <p className="text-secondary text-muted">Floating layers take a shadow. The side panel sits beside the list from 1024 px and opens as a drawer under it.</p>
      </Card>
      <SidePanel open={panel} onOpenChange={setPanel} title="hel-1" description="Hetzner, Helsinki · 4 cores">
        <p className="text-body text-muted">The picked row’s detail, beside the list.</p>
      </SidePanel>
      <Dialog
        open={open === 'dialog'}
        onOpenChange={(value) => setOpen(value ? 'dialog' : null)}
        title="Rent a disposable VM?"
        description="It costs €0.0060 an hour and deletes itself by 16:21."
        footer={
          <>
            <Button onClick={close}>Cancel</Button>
            <Button variant="primary" onClick={close}>Rent it</Button>
          </>
        }
      >
        <IfYes>€0.02 at most, deletes by 16:21</IfYes>
      </Dialog>
      <ConfirmDialog
        open={open === 'confirm'}
        onOpenChange={(value) => setOpen(value ? 'confirm' : null)}
        title="Close Black Friday offer page?"
        description="Its machine and browser are deleted. Its work is saved first."
        confirmLabel="Close it"
        danger
        onConfirm={close}
      />
      <Drawer open={open === 'drawer'} onOpenChange={(value) => setOpen(value ? 'drawer' : null)} title="Run r_8f2a" description="Started 14:09 by Claude Code">
        <p className="text-body text-muted">A drawer holds the plumbing: ids, hosts and engines, behind Technical details.</p>
      </Drawer>
    </div>
  );
}
