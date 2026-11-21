import { useBlocker } from '@tanstack/react-router';
import { ConfirmDialog } from '@/components/ui/dialog';

/**
 * Asks before a person leaves a form with changes that are not saved (web/DESIGN.md, Forms). Inside the
 * app it is a dialog; closing or reloading the tab gets the browser's own question.
 */
export function UnsavedChanges({ when, what = 'your changes' }: { when: boolean; what?: string }) {
  const blocker = useBlocker({
    shouldBlockFn: ({ current, next }) => when && current.pathname !== next.pathname,
    enableBeforeUnload: () => when,
    withResolver: true,
    disabled: !when,
  });
  return (
    <ConfirmDialog
      open={blocker.status === 'blocked'}
      onOpenChange={(open) => {
        if (!open) blocker.reset?.();
      }}
      title="Leave without saving?"
      description={`You changed ${what} and did not save. If you leave now, the change is lost.`}
      confirmLabel="Leave"
      cancelLabel="Stay"
      danger
      onConfirm={() => blocker.proceed?.()}
    />
  );
}
