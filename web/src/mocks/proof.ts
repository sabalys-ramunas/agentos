/**
 * The mock of Proof (docs/API.md, "Audit"): a day of one account replayed from the org's history,
 * the log check, and read-only share links. The same routes and refusals as the node. A share's
 * token is read with no sign-in, and a bad or ended one answers 404 `share_invalid`.
 */
import { http, HttpResponse } from 'msw';
import type {
  Principal,
  ProofChain,
  ProofChainStatus,
  ProofShare,
  ProofShareInput,
  ProofStep,
  TraceEvent,
} from '@/api/types';
import { postPreview } from './art';
import type { MockDb, OrgData } from './seed';
import { scenario } from './scenario';
import type { Helpers } from './s3';

/** The last rows of a day are not covered by a checkpoint yet, so the mock shows both states when it can. */
const UNSIGNED_TAIL = 3;
const SHARE_TTL_MS: Record<string, number> = { '24h': 86_400_000, '7d': 7 * 86_400_000 };

const dayOf = (at: string) => at.slice(0, 10);
const todayOf = () => new Date().toISOString().slice(0, 10);

/** Whether the mock's clock has moved past a share's end. */
const endedAt = (expires: string) => Date.parse(expires) <= Date.now();

function stateOf(share: ProofShare): ProofShare['state'] {
  if (share.revoked_at) return 'revoked';
  return endedAt(share.expires_at) ? 'expired' : 'active';
}

function laneEvents(data: OrgData, lane: string, day: string): TraceEvent[] {
  return data.activity
    .filter((event) => event.lane === lane && dayOf(event.at) === day)
    .sort((a, b) => a.at.localeCompare(b.at));
}

/** One step of the day, shaped as the node shapes it. Rows before the tail are signed. */
function stepsOf(org: string, events: TraceEvent[], screenshots: boolean, token?: string): ProofStep[] {
  const tail = Math.min(UNSIGNED_TAIL, Math.floor(events.length / 2));
  return events.map((event, index) => {
    const unsigned = index >= events.length - tail;
    const status: ProofChainStatus = unsigned ? 'unprovable' : 'ok';
    const principal: Principal = typeof event.principal === 'string' ? { kind: 'agent', id: event.principal } : event.principal;
    const withShot = screenshots && event.run_id !== null;
    const shot = withShot
      ? token
        ? `/api/orgs/local/audit/shared/${token}/runs/${event.run_id}/screenshot`
        : `/api/orgs/${org}/audit/runs/${event.run_id}/screenshot`
      : null;
    return {
      id: event.id,
      seq: 48_000 + index,
      at: event.at,
      ended_at: null,
      kind: 'call',
      tool: event.tool,
      effect: event.effect,
      status: event.status,
      summary: event.summary,
      error: event.status === 'failed' ? 'The page did not show the change.' : null,
      duration_ms: 800 + index * 37,
      principal,
      grant: { id: token ? null : `gr_${index}`, label: `${principal.kind === 'human' ? 'Demo owner' : 'Claude Code'}, up to commit` },
      run: event.run_id
        ? { id: event.run_id, flow: null, status: event.status, lane: event.lane, summary: event.summary, proof: null }
        : null,
      screenshot: shot,
      chain: { status, rows: [{ seq: 48_000 + index, status }] },
    };
  });
}

function chainOf(steps: ProofStep[]): ProofChain {
  const worst = steps.some((step) => step.chain.status === 'unprovable') ? 'unprovable' : 'ok';
  const signed = steps.filter((step) => step.chain.status === 'ok').length;
  return {
    status: worst,
    proven_through: signed ? { mark: 1, seq: 48_000 + signed - 1, at: steps[signed - 1]!.at } : null,
    findings: worst === 'unprovable' ? [{ level: 'note', text: 'The last rows are not signed yet. The next checkpoint covers them.' }] : [],
  };
}

/** `data` reads the mock database as it is now, for the one route that is read with no sign-in. */
export function proofHandlers({ org, failure, newId, data }: Pick<Helpers, 'org' | 'failure' | 'newId'> & { data: () => MockDb }) {
  const shareList = (record: OrgData): (ProofShare & { token: string })[] => {
    record.proofShares ??= [];
    return record.proofShares;
  };
  const publicShare = (share: ProofShare) => ({ id: share.id, kind: share.kind, expires_at: share.expires_at });

  return [
    http.get(
      '/api/orgs/:org/audit/days',
      org((record, { request }) => {
        const url = new URL(request.url);
        const day = url.searchParams.get('day') ?? todayOf();
        const timezone = 'UTC';
        const counts = new Map<string, number>();
        for (const event of record.activity) if (dayOf(event.at) === day) counts.set(event.lane, (counts.get(event.lane) ?? 0) + 1);
        return HttpResponse.json({
          day,
          timezone,
          from: `${day}T00:00:00Z`,
          to: `${day}T23:59:59Z`,
          lanes: [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([name, rows]) => ({ lane: name, rows })),
        });
      }),
    ),
    http.get(
      '/api/orgs/:org/audit/lanes/:ws/:name',
      org((record, { request, params }) => {
        const url = new URL(request.url);
        const day = url.searchParams.get('day') ?? todayOf();
        const lane = `${params.ws}/${params.name}`;
        const timezone = 'UTC';
        if (!record.lanes.some((item) => item.id === lane)) return failure(404, 'not_found', 'Not found.');
        const events = laneEvents(record, lane, day);
        const steps = stepsOf(String(params.org), events, true);
        return HttpResponse.json({
          lane,
          day,
          timezone,
          from: `${day}T00:00:00Z`,
          to: `${day}T23:59:59Z`,
          truncated: false,
          chain: chainOf(steps),
          steps,
        });
      }),
    ),
    http.get(
      '/api/orgs/:org/audit/verify',
      org((record) => {
        const rows = record.activity.length;
        const signed = Math.max(0, rows - UNSIGNED_TAIL);
        const tampered = scenario.has('log-changed');
        return HttpResponse.json({
          status: tampered ? 'tampered' : 'ok',
          checked: rows,
          rows: { first: rows ? 1 : null, last: rows || null, in_scope: rows },
          proven_through: signed ? { mark: 1, seq: signed, at: record.activity[signed - 1]?.at ?? new Date().toISOString() } : null,
          chain: tampered ? 'tampered' : rows > signed ? 'unprovable' : 'ok',
          keys: [{ kid: 'ed25519:mock-audit', pub: 'mock', from_mark: 1 }],
          unchained: 0,
          findings: tampered
            ? [{ level: 'tampered', text: 'A row no longer matches the signed checkpoint that covers it.', seq: [1, Math.max(1, signed)], mark: 1 }]
            : [],
        });
      }),
    ),
    http.get(
      '/api/orgs/:org/audit/shares',
      org((record) => HttpResponse.json(shareList(record).map(({ token: _token, ...share }) => ({ ...share, state: stateOf(share) })))),
    ),
    http.post(
      '/api/orgs/:org/audit/shares',
      org(async (record, { request }) => {
        const body = (await request.json()) as ProofShareInput;
        if (!body.trace && !(body.lane && body.day)) return failure(400, 'invalid', 'Pick one step, or an account and a day.');
        const ttl = body.ttl ?? '24h';
        if (!(ttl in SHARE_TTL_MS)) return failure(400, 'invalid', 'A link lasts 24 hours or 7 days.');
        if (body.lane && !record.lanes.some((item) => item.id === body.lane)) return failure(404, 'not_found', 'Not found.');
        const id = newId('sh');
        const token = `shr_mock_${id}`;
        const expires = new Date(Date.now() + SHARE_TTL_MS[ttl]!).toISOString();
        const share: ProofShare & { token: string } = {
          id,
          kind: body.trace ? 'trace' : 'lane_day',
          target: body.trace ? { trace: body.trace } : { lane: body.lane, day: body.day, timezone: 'UTC' },
          screenshots: Boolean(body.screenshots),
          state: 'active',
          created_at: new Date().toISOString(),
          expires_at: expires,
          revoked_at: null,
          by: 'owner:panel',
          views: 0,
          last_view_at: null,
          token,
        };
        shareList(record).unshift(share);
        const { token: _token, ...rest } = share;
        return HttpResponse.json(
          {
            ...rest,
            token,
            url: `https://${new URL(location.href).host}/shared#token=${token}`,
            api_url: `/api/audit/shared/${token}`,
          },
          { status: 201 },
        );
      }),
    ),
    http.delete(
      '/api/orgs/:org/audit/shares/:id',
      org((record, { params }) => {
        const share = shareList(record).find((item) => item.id === params.id);
        if (!share) return failure(404, 'not_found', 'No such link.');
        share.revoked_at ??= new Date().toISOString();
        const { token: _token, ...rest } = share;
        return HttpResponse.json({ ...rest, state: stateOf(share) });
      }),
    ),
    http.get(
      '/api/orgs/:org/audit/runs/:id/screenshot',
      org((_record, { params }) => new HttpResponse(postPreview(String(params.id)), { headers: { 'Content-Type': 'image/svg+xml' } })),
    ),
    // A read-only link: no sign-in, and the token is the only credential.
    http.get('/api/orgs/:org/audit/shared/:token', async ({ params }) => {
      const token = String(params.token);
      for (const record of data().orgs.values()) {
        const share = (record.proofShares ?? []).find((item) => item.token === token);
        if (!share) continue;
        if (stateOf(share) !== 'active') break;
        share.views += 1;
        share.last_view_at = new Date().toISOString();
        if (share.kind === 'trace') {
          const id = String(share.target.trace);
          const event = record.activity.find((item) => item.id === id);
          if (!event) break;
          const [step] = stepsOf('local', [event], share.screenshots, token);
          return HttpResponse.json({ share: publicShare(share), trace: id, lane: event.lane, step, chain: chainOf([step!]) });
        }
        const events = laneEvents(record, String(share.target.lane), String(share.target.day));
        const steps = stepsOf('local', events, share.screenshots, token);
        return HttpResponse.json({
          share: publicShare(share),
          lane: share.target.lane,
          day: share.target.day,
          timezone: 'UTC',
          from: `${share.target.day}T00:00:00Z`,
          to: `${share.target.day}T23:59:59Z`,
          truncated: false,
          chain: chainOf(steps),
          steps,
        });
      }
      return HttpResponse.json({ error: { code: 'share_invalid', message: 'This link is not valid. It may have ended or been revoked.' } }, { status: 404 });
    }),
    http.get('/api/orgs/:org/audit/shared/:token/runs/:id/screenshot', () => new HttpResponse(postPreview('shared'), { headers: { 'Content-Type': 'image/svg+xml' } })),
  ];
}
