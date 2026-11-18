/**
 * The design system's catalog (src/catalog): every component in every state. Dev only, never linked from
 * the app. The route sits behind import.meta.env.DEV where it is made, so a live build folds it away with
 * the catalog's code; build/live-build.test.ts reads a build to hold that.
 */
import { createRoute, lazyRouteComponent } from '@tanstack/react-router';
import { orgRoute } from './org';

export const catalogRoutes = import.meta.env.DEV
  ? [
      createRoute({
        getParentRoute: () => orgRoute,
        path: '/design',
        component: lazyRouteComponent(() => import('@/catalog/catalog-page'), 'CatalogPage'),
      }),
    ]
  : [];
