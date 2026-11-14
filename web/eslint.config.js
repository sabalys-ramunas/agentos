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
    files: ['scripts/**/*.mjs', 'e2e/**', 'build/**', '*.config.*'],
    languageOptions: { globals: globals.node },
  },
);
