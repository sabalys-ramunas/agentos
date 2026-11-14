import js from '@eslint/js';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'public', 'screenshots', 'playwright-report', 'test-results'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  jsxA11y.flatConfigs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: { globals: globals.browser },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      '@typescript-eslint/consistent-type-imports': 'error',
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: 'Node-supplied text is rendered as text, never as HTML.',
        },
      ],
      // autoFocus is deliberate in dialogs and single-purpose auth forms, where the first
      // field is the whole point of the screen.
      'jsx-a11y/no-autofocus': 'off',
      // Kept readable: a function with more paths than this is split into named parts.
      complexity: ['error', 25],
      'max-depth': ['error', 4],
      'max-lines': ['error', { max: 500, skipBlankLines: true, skipComments: true }],
    },
  },
  {
    files: ['src/features/**', 'src/components/**', 'src/app/**'],
    // Tests may seed and inspect the mock. It is the screens that must stay unaware of it.
    ignores: ['**/*.test.*'],
    rules: {
      'no-restricted-imports': [
        'error',
        { patterns: [{ group: ['@/mocks/*', '**/mocks/*'], message: 'Screens never know about the mock backend.' }] },
      ],
    },
  },
  {
    // Mocks are a fictional world written out by hand, and tests read best as one file per screen,
    // so neither is held to a file length.
    files: ['src/mocks/**', '**/*.test.*', 'e2e/**', 'e2e-live/**'],
    rules: { 'max-lines': 'off' },
  },
  {
    // Files still over the limits when the rules came in (2026-11-14). This list may only shrink:
    // fix a file and take it off, and never add one. A file here is held to every other rule.
    files: [
      'src/components/charts/time-chart.tsx',
      'src/components/shell/status-pill.tsx',
      'src/features/activity/activity.tsx',
      'src/features/activity/story-view.ts',
      'src/features/activity/story.ts',
      'src/features/auth/invite.tsx',
      'src/features/brand/model.ts',
      'src/features/brand/sections.tsx',
      'src/features/checks/check-lower.tsx',
      'src/features/checks/check-page.tsx',
      'src/features/email/list.tsx',
      'src/features/flows/builder.tsx',
      'src/features/flows/flow-detail.tsx',
      'src/features/flows/flows.tsx',
      'src/features/flows/inspector.tsx',
      'src/features/home/words.ts',
      'src/features/lane/control.tsx',
      'src/features/lane/sign-in.tsx',
      'src/features/needs-you/approve.tsx',
      'src/features/needs-you/if-yes.tsx',
      'src/features/needs-you/kinds.ts',
      'src/features/needs-you/words.ts',
      'src/features/nodes/header.tsx',
      'src/features/nodes/machines.tsx',
      'src/features/nodes/node-detail.tsx',
      'src/features/nodes/tab-backups.tsx',
      'src/features/onboarding/step-accounts.tsx',
      'src/features/onboarding/step-computer.tsx',
      'src/features/onboarding/step-goal.tsx',
      'src/features/onboarding/step-try.tsx',
      'src/features/runs/failures.ts',
      'src/features/runs/kind.ts',
      'src/features/settings/digest-preview.tsx',
      'src/features/settings/push-device.tsx',
      'src/features/site-apis/site-api-detail.tsx',
      'src/features/site-apis/split.tsx',
      'src/features/standing/standing-dialog.tsx',
      'src/features/workers/add-service.tsx',
      'src/lib/principal.ts',
      'src/lib/states.ts',
      'src/mocks/fleet-preview.ts',
      'src/mocks/fleet.ts',
      'src/mocks/history.ts',
      'src/mocks/routines.ts',
      'src/mocks/s3.ts',
    ],
    rules: { complexity: 'off' },
  },
  {
    // The one typed client: tests/unit/web/test_every_call.py and scripts/contract.mjs read every
    // call from this one file, so it stays whole. This list may only shrink.
    files: ['src/api/client.ts'],
    rules: { 'max-lines': 'off' },
  },
  {
    files: ['scripts/**/*.mjs', 'e2e/**', 'build/**', '*.config.*'],
    languageOptions: { globals: globals.node },
  },
);
