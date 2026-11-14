import { useMemo } from 'react';
import { createStored } from './store';

const ALL = '';
const stores = new Map<string, ReturnType<typeof createStored<string>>>();

function storeFor(org: string) {
  let store = stores.get(org);
  if (!store) {
    store = createStored<string>(`agentos.workspace.${org}`, ALL);
    stores.set(org, store);
  }
  return store;
}

/** The workspace switcher's choice for this org. An empty string means all of them. */
export function useActiveWorkspace(org: string) {
  const store = useMemo(() => storeFor(org), [org]);
  const value = store.use();
  return [value, store.set] as const;
}
