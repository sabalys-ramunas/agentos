import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const WITH_FILES = '/northwind-group/accounts/aster/instagram';
const NO_FILES = '/northwind-group/accounts/meridian/instagram';
const TERMINAL = '/northwind-group/accounts/personal/shell';

/** The mock org's role for the signed-in person, put back after each test that changes it. */
function setRole(role: 'viewer' | 'owner') {
  const org = db.orgs.get('northwind-group')!;
  const before = org.role;
  org.role = role;
  return () => {
    org.role = before;
  };
}

async function filesRegion() {
  return within(await screen.findByRole('region', { name: 'Files' }, { timeout: 4000 }));
}

describe('files on an account', () => {
  it('lists the inbox and the outbox with what they may hold, and saves a download as the file it is', async () => {
    const url = URL as unknown as Record<string, unknown>;
    url.createObjectURL = vi.fn(() => 'blob:latte');
    url.revokeObjectURL = vi.fn();
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    try {
      const { user } = renderApp(WITH_FILES);
      const files = await filesRegion();
      expect(files.getByText('Ready to use')).toBeInTheDocument();
      expect(files.getByText('autumn-latte.jpg')).toBeInTheDocument();
      expect(files.getByText(/Each file can be up to 200\.0 MB\./)).toBeInTheDocument();
      await user.click(files.getByRole('button', { name: 'Download autumn-latte.jpg' }));
      await waitFor(() => expect(clicked).toHaveBeenCalledTimes(1));
      const link = clicked.mock.contexts[0] as HTMLAnchorElement;
      expect(link.download).toBe('autumn-latte.jpg');
    } finally {
      clicked.mockRestore();
      delete url.createObjectURL;
      delete url.revokeObjectURL;
    }
  });

  it('adds a file to the inbox, and refuses one over the limit before it is sent', async () => {
    const { user } = renderApp(WITH_FILES);
    const files = await filesRegion();
    const input = files.getByRole('button', { name: 'Add a file' }).parentElement!.querySelector('input[type=file]') as HTMLInputElement;
    await user.upload(input, new File(['Pumpkin flat white menu'], 'menu-notes.txt', { type: 'text/plain' }));
    expect(await files.findByText('menu-notes.txt')).toBeInTheDocument();

    const big = new File(['x'], 'raw-video.bin', { type: 'application/octet-stream' });
    Object.defineProperty(big, 'size', { value: 300 * 1024 * 1024 });
    await user.upload(input, big);
    expect(await files.findByRole('alert')).toHaveTextContent('raw-video.bin is larger than the 200.0 MB limit, so it was not added.');
    expect(files.queryByText('raw-video.bin')).not.toBeInTheDocument();
  });

  it('deletes a file only after the person confirms it', async () => {
    const { user } = renderApp(WITH_FILES);
    const files = await filesRegion();
    const input = files.getByRole('button', { name: 'Add a file' }).parentElement!.querySelector('input[type=file]') as HTMLInputElement;
    await user.upload(input, new File(['temporary'], 'temporary.txt', { type: 'text/plain' }));
    await user.click(await files.findByRole('button', { name: 'Delete temporary.txt' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Delete temporary.txt?' }));
    await user.click(dialog.getByRole('button', { name: 'Delete it' }));
    await waitFor(() => expect(files.queryByText('temporary.txt')).not.toBeInTheDocument());
    expect(files.getByText('autumn-latte.jpg')).toBeInTheDocument();
  });

  it('lets a viewer save a file, and offers neither an add nor a delete', async () => {
    const back = setRole('viewer');
    try {
      renderApp(WITH_FILES);
      const files = await filesRegion();
      expect(files.getByRole('button', { name: 'Download autumn-latte.jpg' })).toBeInTheDocument();
      expect(files.queryByRole('button', { name: 'Add a file' })).not.toBeInTheDocument();
      expect(files.queryByRole('button', { name: /^Delete / })).not.toBeInTheDocument();
    } finally {
      back();
    }
  });

  it('is left off a viewer\'s account page when there is nothing to save or add', async () => {
    const back = setRole('viewer');
    try {
      renderApp(NO_FILES);
      expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument();
      expect(screen.queryByRole('region', { name: 'Files' })).not.toBeInTheDocument();
    } finally {
      back();
    }
  });

  it('is only for browser accounts, so a shell shows no files at all', async () => {
    renderApp(TERMINAL);
    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Files' })).not.toBeInTheDocument();
  });
});
