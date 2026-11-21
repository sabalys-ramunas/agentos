import { act, screen, waitFor, within } from '@testing-library/react';
import { startPwa } from '@/lib/pwa';
import { renderApp } from '@/test/render';


function offerInstall() {
  const event = Object.assign(new Event('beforeinstallprompt', { cancelable: true }), {
    prompt: vi.fn(async () => undefined),
    userChoice: Promise.resolve({ outcome: 'accepted' as const }),
  });
  act(() => void window.dispatchEvent(event));
  return event;
}

beforeAll(() => startPwa());
beforeEach(() => sessionStorage.clear());

describe('the install prompt', () => {
  it('holds the browser banner, stays away on arrival, and comes after the first decision', async () => {
    const { user } = renderApp('/northwind-group/needs-you');
    await screen.findByRole('region', { name: 'Posts to check' });
    const event = offerInstall();
    expect(event.defaultPrevented).toBe(true);
    expect(screen.queryByRole('complementary', { name: 'Install openajar' })).not.toBeInTheDocument();
    // The first decision earns it: a no to the payment in front.
    await user.click(await screen.findByRole('button', { name: "Don't pay" }));
    const prompt = within(await screen.findByRole('complementary', { name: 'Install openajar' }));
    await user.click(prompt.getByRole('button', { name: 'Install' }));
    expect(event.prompt).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('complementary', { name: 'Install openajar' })).not.toBeInTheDocument());
  });

  it('rests for a month after Not now', async () => {
    sessionStorage.setItem('agentos.install.earned', '1');
    const { user } = renderApp('/northwind-group/needs-you');
    await screen.findByRole('region', { name: 'Posts to check' });
    offerInstall();
    const prompt = within(await screen.findByRole('complementary', { name: 'Install openajar' }));
    await user.click(prompt.getByRole('button', { name: 'Not now' }));
    expect(screen.queryByRole('complementary', { name: 'Install openajar' })).not.toBeInTheDocument();
    expect(Number(localStorage.getItem('agentos.install.dismissed-at'))).toBeGreaterThan(0);
  });
});
