import '@fontsource-variable/geist/wght.css';
import '@fontsource-variable/geist-mono/wght.css';
import './styles/app.css';
import './lib/input-modality';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './app/app';
import { startPwa } from './lib/pwa';

async function boot() {
  // A literal comparison, so a live build drops the mock backend entirely.
  if (import.meta.env.VITE_API_MODE === 'mock') {
    const { startMockApi } = await import('./mocks/browser');
    await startMockApi();
  }
  startPwa();
  const root = document.getElementById('root');
  if (root) {
    createRoot(root).render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  }
}

void boot();
