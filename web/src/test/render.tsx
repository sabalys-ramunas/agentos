import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { Toaster, toast } from 'sonner';
import { makeRouter } from '@/app/router';
import { StepUpDialog } from '@/components/shell/step-up-dialog';
import { TooltipProvider } from '@/components/ui/menu';

/** Renders the real router and shell at a path, against the same handlers the app uses. */
export function renderApp(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = makeRouter(client, createMemoryHistory({ initialEntries: [path] }));
  const view = render(
    // StrictMode, as in main.tsx: effects run twice, which is what the one-time token test needs.
    <StrictMode>
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <RouterProvider router={router} />
          <StepUpDialog />
        </TooltipProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
  return { ...view, client, router, user: userEvent.setup() };
}

/**
 * The shell's toast stack, for a test that reads a toast (renderApp leaves it out, as the app mounts it beside
 * the router). Sonner holds the pointer when a toast is pressed; jsdom has no pointer capture, so it is stubbed.
 */
export function renderToasts() {
  Element.prototype.setPointerCapture ??= () => undefined;
  Element.prototype.releasePointerCapture ??= () => undefined;
  // Toasts from an earlier test are still in sonner's store; a new stack would show them again.
  toast.dismiss();
  return render(<Toaster toastOptions={{ unstyled: true }} />);
}
