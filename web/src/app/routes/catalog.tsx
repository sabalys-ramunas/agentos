/**
 * The design system's catalog (src/catalog): every component in every state. Dev only, never linked from
 * the app. The route is pure, so a live build that leaves it out also drops the catalog's code, and
 * build/live-build.test.ts reads the build to hold that.
 */
import { createRoute, lazyRouteComponent } from '@tanstack/react-router';
import { orgRoute } from './org';

export const catalogRoute = /* @__PURE__ */ createRoute({
  getParentRoute: () => orgRoute,
  path: '/design',
  component: lazyRouteComponent(() => import('@/catalog/catalog-page'), 'CatalogPage'),
});
