/**
 * The mock of the overrides the node keeps (docs/API.md): a safety check a person turned off for an
 * account, with the reason they typed. The owner alone reads them, as the node refuses anyone else.
 */
import { http, HttpResponse } from 'msw';
import type { Override } from '@/api/types';
import type { Helpers } from './s3';

const DAY = 86_400_000;

export function overrideHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const seeded: Override[] = [
    {
      id: 2,
      at: new Date(Date.now() - 2 * DAY).toISOString(),
      by: 'owner:panel',
      lane: 'aster/instagram',
      proxy: 'vilnius-1',
      rules: ['country_match'],
      reason: 'The account is in Vilnius and its only connection is in Frankfurt for the shoot week.',
    },
    {
      id: 1,
      at: new Date(Date.now() - 9 * DAY).toISOString(),
      by: 'owner:panel',
      lane: 'harbor/shop',
      proxy: null,
      rules: ['shared_address'],
      reason: 'Two accounts share one address on purpose.',
    },
  ];
  return [
    http.get(
      '/api/orgs/:org/overrides',
      org((record, { request }) => {
        if (record.role !== 'owner') return failure(403, 'denied', 'Only the owner sees the checks that were turned off.');
        const limit = Number(new URL(request.url).searchParams.get('limit') ?? 50);
        return HttpResponse.json(seeded.slice(0, limit));
      }),
    ),
  ];
}
