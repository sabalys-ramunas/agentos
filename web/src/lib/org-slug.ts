/** The same rule as `check_org_slug` in the API, plus the panel's own top-level routes. */
const SHAPE = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/;

// The API reserves the first row. The second row would shadow a page in this panel.
const RESERVED = new Set([
  'admin', 'api', 'app', 'auth', 'docs', 'invite', 'new', 'settings', 'sign-in', 'www',
  'check-email', 'computer-form', 'legal', 'onboarding', 'sign-up', 'v1',
]);

export function orgSlugProblem(slug: string): string | null {
  if (!SHAPE.test(slug)) return 'Use 3 to 40 lowercase letters, digits or dashes.';
  return RESERVED.has(slug) ? 'That address is reserved. Try another.' : null;
}
