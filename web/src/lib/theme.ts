import { createStored } from './store';

export type ThemeChoice = 'system' | 'light' | 'dark';

const stored = createStored<ThemeChoice>('agentos.theme', 'system');

export const useTheme = stored.use;

export function setTheme(choice: ThemeChoice) {
  stored.set(choice);
  if (choice === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
}
