/**
 * The mock of S3: phone push, Review links, webhooks, standing approvals, retention and export.
 * Same routes, shapes, step-up rules and refusals as the API has (src/api/client.ts and types.ts).
 */
import { http, HttpResponse } from 'msw';
import type {
  Approval,
  Effect,
  NewStandingApproval,
  StandingApproval,
  WebhookDelivery,
  WebhookEvent,
} from '@/api/types';
import { withPlace } from './rebuild';
import { scenario } from './scenario';
import { notApplied } from './seed';
import type { OrgData } from './seed';
import { principalId } from '@/lib/principal';

interface Info {
  request: Request;
  params: Record<string, string | readonly string[] | undefined>;
}

export interface Helpers {
  org: (resolver: (data: OrgData, info: Info) => Response | Promise<Response>) => (info: Info) => Promise<Response>;
  failure: (status: number, code: string, message: string, extra?: Record<string, unknown>) => Response;
  stepUpNeeded: () => Response;
  stepUpValid: () => boolean;
  newId: (prefix: string) => string;
}

const DAY = 86_400_000;
const LINK_MS = 30 * 60_000;
const EVENTS: WebhookEvent[] = ['approval.requested', 'approval.decided', 'approval.expired', 'run.failed'];
const REFUSED_EFFECTS = new Set<Effect>(['pay', 'irreversible', 'install_base']);
const RANK = { owner: 4, admin: 3, member: 2, approver: 1, viewer: 0, auditor: 0 } as const;

/** A real VAPID public key shape: 65 bytes, base64url. The mock never sends with it. */
const PUBLIC_KEY = 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U';

/** Mock Review links carry this instead of a real signature. `forged` reads as a bad one. */
export const MOCK_SIGNATURE = 'mock';

export function reviewPath(org: string, id: string, now = Date.now()) {
  return `/${org}/review/${id}?e=${Math.floor((now + LINK_MS) / 1000)}&s=${MOCK_SIGNATURE}`;
}

/**
 * The browser mock has no push service. A test push goes straight to the service worker, which
 * shows it exactly as it would a real one (`sw-mock.js` listens for this).
 */
export function deliverMockPush(payload: Record<string, unknown>) {
  const worker = typeof navigator !== 'undefined' ? navigator.serviceWorker?.controller : null;
  worker?.postMessage({ type: 'openajar-mock-push', payload });
}

function isPrivate(host: string) {
  return /^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.|\[?::1\]?$)/.test(host) || host.endsWith('.internal') || host.endsWith('.local');
}

function checkWebhookUrl(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return 'That is not a web address.';
  }
  if (url.protocol !== 'https:') return 'Use an https address. Webhooks are never sent over plain http.';
  if (url.port && url.port !== '443' && url.port !== '8443') return 'Use port 443 or 8443.';
  if (isPrivate(url.hostname)) return 'That address is private, so openajar will not send to it.';
  return null;
}

function secret() {
  const bytes = Array.from({ length: 24 }, () => Math.floor(Math.random() * 256));
  return `whsec_${btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')}`;
}

function nodesFor(data: OrgData, workspace: string, lanes: string[] | null): string[] {
  const covered = data.lanes.filter(
    (lane) => lane.workspace === workspace && (!lanes || lanes.some((pattern) => (pattern.endsWith('*') ? lane.id.startsWith(pattern.slice(0, -1)) : lane.id === pattern))),
  );
  return [...new Set(covered.map((lane) => lane.node))].sort();
}

function csvCell(value: unknown) {
  const text = value === null || value === undefined ? '' : String(value);
  const plain = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return /[",\n]/.test(plain) ? `"${plain.replaceAll('"', '""')}"` : plain;
}

export function s3Handlers({ org, failure, stepUpNeeded, stepUpValid, newId }: Helpers) {
  const notFound = () => failure(404, 'not_found', 'Not found.');
  const allowed = (data: OrgData, least: keyof typeof RANK) => RANK[data.role] >= RANK[least];
  const forbidden = () => failure(403, 'forbidden', 'You do not have access to do that.');
  const json = async <T,>(request: Request) => (await request.clone().json()) as T;
  const findApproval = (data: OrgData, id: string): Approval | undefined =>
    data.approvals.find((item) => item.id === id) ?? data.history.find((item) => item.id === id);

  return [
    // ---- push ----
    http.get(
      '/api/orgs/:org/push/config',
      org((data) => {
        // `?mock=push-off` is a server with no VAPID key; a Free org's plan has no push.
        if (scenario.has('push-off')) return HttpResponse.json({ available: false, reason: 'not_configured', public_key: null });
        if (data.org.plan === 'Free') return HttpResponse.json({ available: false, reason: 'plan', public_key: PUBLIC_KEY });
        return HttpResponse.json({ available: true, reason: null, public_key: PUBLIC_KEY });
      }),
    ),
    http.get(
      '/api/orgs/:org/push/devices',
      org((data) => HttpResponse.json(data.pushDevices.map(({ endpoint: _endpoint, ...device }) => device))),
    ),
    http.post(
      '/api/orgs/:org/push/devices',
      org(async (data, { request }) => {
        if (scenario.has('push-off')) return failure(409, 'conflict', 'Phone push is not set up on this server.');
        if (data.org.plan === 'Free') return failure(402, 'plan_limit', 'Your plan does not include phone push.', { limit: 0, current: 0 });
        const body = await json<{ endpoint: string; keys: { p256dh: string; auth: string } }>(request);
        if (!body.endpoint?.startsWith('https://') || !body.keys?.p256dh || !body.keys?.auth) {
          return failure(400, 'invalid', 'That subscription is not complete.');
        }
        const existing = data.pushDevices.find((item) => item.endpoint === body.endpoint);
        if (existing) {
          const { endpoint: _endpoint, ...device } = existing;
          return HttpResponse.json(device, { status: 201 });
        }
        if (data.pushDevices.length >= 10) return failure(409, 'conflict', 'You have 10 devices. Remove one first.');
        const label = /iPhone|iPad/.test(navigator.userAgent) ? 'Safari on iPhone' : /Android/.test(navigator.userAgent) ? 'Chrome on Android' : 'Chrome on macOS';
        const device = { id: newId('dev'), label, endpoint: body.endpoint, created_at: new Date().toISOString(), last_sent_at: null };
        data.pushDevices.unshift(device);
        const { endpoint: _endpoint, ...out } = device;
        return HttpResponse.json(out, { status: 201 });
      }),
    ),
    http.delete(
      '/api/orgs/:org/push/devices/:id',
      org((data, { params }) => {
        const before = data.pushDevices.length;
        data.pushDevices = data.pushDevices.filter((item) => item.id !== params.id);
        return data.pushDevices.length < before ? new HttpResponse(null, { status: 204 }) : notFound();
      }),
    ),
    http.post(
      '/api/orgs/:org/push/test',
      org((data) => {
        if (data.pushDevices.length === 0) return failure(409, 'conflict', 'Turn on notifications on a device first.');
        const now = new Date().toISOString();
        data.pushDevices.forEach((item) => (item.last_sent_at = now));
        deliverMockPush({
          v: 1,
          type: 'test',
          org: data.org.slug,
          tag: `test-${Date.now()}`,
          title: 'Notifications are working',
          body: 'Approvals that need you will reach this device.',
          url: `/${data.org.slug}/settings/notifications`,
          fallback_url: `/${data.org.slug}/settings/notifications`,
          urgent: false,
        });
        return HttpResponse.json({ devices: data.pushDevices.length }, { status: 202 });
      }),
    ),

    // ---- Review links ----
    http.get(
      '/api/orgs/:org/approvals/:id/review',
      org((data, { params, request }) => {
        const search = new URL(request.url).searchParams;
        const expires = Number(search.get('e'));
        const approval = findApproval(data, String(params.id));
        if (!approval) return notFound();
        if (search.get('s') !== MOCK_SIGNATURE || !Number.isFinite(expires) || expires * 1000 <= Date.now()) {
          return failure(410, 'expired', 'This link has expired.');
        }
        return HttpResponse.json(withPlace(data, approval));
      }),
    ),

    // ---- webhooks ----
    http.get('/api/orgs/:org/webhooks', org((data) => HttpResponse.json(data.webhooks))),
    http.post(
      '/api/orgs/:org/webhooks',
      org(async (data, { request }) => {
        if (!allowed(data, 'member')) return forbidden();
        if (!stepUpValid()) return stepUpNeeded();
        const body = await json<{ url: string; events?: WebhookEvent[] }>(request);
        const refused = checkWebhookUrl(body.url ?? '');
        if (refused) return failure(400, 'invalid', refused);
        const limit = data.org.plan === 'Free' ? 1 : 5;
        if (data.webhooks.length >= limit) {
          return failure(402, 'plan_limit', `Your plan includes ${limit} ${limit === 1 ? 'webhook' : 'webhooks'} each.`, { limit, current: data.webhooks.length });
        }
        const events = body.events?.length ? [...new Set(body.events)].sort() : [...EVENTS];
        const hook = { id: newId('wh'), origin: new URL(body.url).origin, events, enabled: true, created_at: new Date().toISOString(), last_delivery: null };
        data.webhooks.unshift(hook);
        data.deliveries[hook.id] = [];
        return HttpResponse.json({ ...hook, secret: secret() }, { status: 201 });
      }),
    ),
    http.patch(
      '/api/orgs/:org/webhooks/:id',
      org(async (data, { params, request }) => {
        const hook = data.webhooks.find((item) => item.id === params.id);
        if (!hook) return notFound();
        if (!allowed(data, 'member')) return forbidden();
        if (!stepUpValid()) return stepUpNeeded();
        const body = await json<{ enabled?: boolean; events?: WebhookEvent[] }>(request);
        if (body.events) {
          if (!body.events.length || body.events.some((item) => !EVENTS.includes(item))) return failure(400, 'invalid', `Events are: ${EVENTS.join(', ')}.`);
          hook.events = [...new Set(body.events)].sort();
        }
        if (body.enabled !== undefined) hook.enabled = body.enabled;
        return HttpResponse.json(hook);
      }),
    ),
    http.delete(
      '/api/orgs/:org/webhooks/:id',
      org((data, { params }) => {
        if (!data.webhooks.some((item) => item.id === params.id)) return notFound();
        data.webhooks = data.webhooks.filter((item) => item.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    ),
    http.post(
      '/api/orgs/:org/webhooks/:id/rotate',
      org((data, { params }) => {
        if (!data.webhooks.some((item) => item.id === params.id)) return notFound();
        if (!allowed(data, 'member')) return forbidden();
        if (!stepUpValid()) return stepUpNeeded();
        return HttpResponse.json({ secret: secret() });
      }),
    ),
    http.post(
      '/api/orgs/:org/webhooks/:id/test',
      org((data, { params }) => {
        const hook = data.webhooks.find((item) => item.id === params.id);
        if (!hook) return notFound();
        if (!allowed(data, 'member')) return forbidden();
        if (!stepUpValid()) return stepUpNeeded();
        const delivery: WebhookDelivery = {
          id: newId('whd'),
          kind: 'ping',
          state: 'pending',
          attempts: 0,
          status: null,
          error: null,
          next_attempt_at: new Date().toISOString(),
          created_at: new Date().toISOString(),
          delivered_at: null,
        };
        (data.deliveries[hook.id] ??= []).unshift(delivery);
        hook.last_delivery = delivery;
        // The worker sends it a moment later, like the real queue.
        setTimeout(() => {
          Object.assign(delivery, { state: 'delivered', attempts: 1, status: 200, next_attempt_at: null, delivered_at: new Date().toISOString() });
        }, scenario.has('still') ? 0 : 1200);
        return HttpResponse.json({ id: delivery.id }, { status: 202 });
      }),
    ),
    http.get(
      '/api/orgs/:org/webhooks/:id/deliveries',
      org((data, { params }) => {
        if (!data.webhooks.some((item) => item.id === params.id)) return notFound();
        return HttpResponse.json(data.deliveries[String(params.id)] ?? []);
      }),
    ),

    // ---- standing approvals ----
    http.get(
      '/api/orgs/:org/standing-approvals',
      org((data, { request }) => {
        const all = new URL(request.url).searchParams.get('all') === 'true';
        return HttpResponse.json(all ? data.standing : data.standing.filter((item) => item.state === 'active'));
      }),
    ),
    http.get(
      '/api/orgs/:org/standing-approvals/support',
      org((data, { request }) => {
        const workspace = new URL(request.url).searchParams.get('workspace') ?? '';
        if (!data.workspaces.some((item) => item.id === workspace)) return notFound();
        return HttpResponse.json(notApplied(nodesFor(data, workspace, null)));
      }),
    ),
    http.post(
      '/api/orgs/:org/standing-approvals',
      org(async (data, { request }) => {
        if (!allowed(data, 'admin')) return forbidden();
        if (!stepUpValid()) return stepUpNeeded();
        const body = await json<NewStandingApproval>(request);
        let { workspace, effect } = body;
        let targets = [...(body.targets ?? [])];
        let lanes = body.lanes ?? null;
        let contents = 0;
        for (const seen of body.from_approvals ?? []) {
          const approval = findApproval(data, seen.id);
          if (!approval) return notFound();
          if (approval.content_hash !== seen.content_hash) return failure(409, 'content_changed', 'This changed since you read it.');
          if (approval.state === 'denied') return failure(400, 'invalid', 'Only an approval you approved, or are looking at now, can be allowed again.');
          const ws = approval.lane.split('/')[0] ?? '';
          effect ??= approval.effect;
          workspace ??= ws;
          if (approval.effect !== effect || ws !== workspace) return failure(400, 'invalid', 'Those approvals are not all the same effect in the same workspace.');
          targets.push(approval.flow_target ?? `tool:${approval.tool ?? 'browser_act'}`);
          if (approval.content_digest) contents += 1;
          if (body.lanes === undefined) lanes = [...new Set([...(lanes ?? []), approval.lane])];
        }
        targets = [...new Set(targets)];
        if (!workspace || !effect) return failure(400, 'invalid', 'Choose a workspace and what this covers.');
        if (REFUSED_EFFECTS.has(effect)) return failure(400, 'invalid', `A standing approval cannot cover ${effect}. A person decides those every time.`);
        if (effect === 'commit' && !targets.length) return failure(400, 'invalid', 'Say what this covers: a flow version or a tool, from an approval you saw.');
        if (targets.some((item) => !/^(flow:[a-z0-9][a-z0-9_.-]*@[A-Za-z0-9._:-]+|tool:[a-z][a-z0-9_]*)$/.test(item))) {
          return failure(400, 'invalid', 'Name a target as flow:<id>@<version> or tool:<name>.');
        }
        if (!data.workspaces.some((item) => item.id === workspace)) return failure(400, 'invalid', 'That workspace does not exist in this org.');
        if (lanes?.some((item) => !item.startsWith(`${workspace}/`))) return failure(400, 'invalid', 'Every lane has to be in that workspace.');
        const until = new Date(body.until).getTime();
        if (!(until > Date.now())) return failure(400, 'invalid', 'Choose a time in the future.');
        if (until > Date.now() + 90 * DAY) return failure(400, 'invalid', 'A standing approval can last 90 days at most.');
        const active = data.standing.filter((item) => item.state === 'active').length;
        if (active >= 10) return failure(402, 'plan_limit', 'Your plan includes 10 active standing approvals.', { limit: 10, current: active });
        const covered = lanes?.length ? lanes : null;
        const record: StandingApproval = {
          id: newId('sa'),
          workspace,
          effect,
          lanes: covered,
          count: body.count,
          used: 0,
          remaining: body.count,
          per_day: body.per_day ?? null,
          until: new Date(until).toISOString(),
          targets,
          contents,
          covers_publishing: targets.length > 0,
          note: body.note?.trim() || null,
          state: 'active',
          created_at: new Date().toISOString(),
          revoked_at: null,
          enforcement: notApplied(nodesFor(data, workspace, covered)),
        };
        data.standing.unshift(record);
        return HttpResponse.json(record, { status: 201 });
      }),
    ),
    http.delete(
      '/api/orgs/:org/standing-approvals/:id',
      org((data, { params }) => {
        if (!allowed(data, 'admin')) return forbidden();
        const record = data.standing.find((item) => item.id === params.id);
        if (!record) return notFound();
        if (record.state === 'revoked') return failure(409, 'conflict', 'That standing approval is already revoked.');
        Object.assign(record, { state: 'revoked', revoked_at: new Date().toISOString() });
        return new HttpResponse(null, { status: 204 });
      }),
    ),

    // ---- retention and export ----
    http.get('/api/orgs/:org/retention', org((data) => HttpResponse.json({ ...data.retention, can_export: data.role === 'owner' }))),
    http.get(
      '/api/orgs/:org/activity/export',
      org((data, { request }) => {
        if (data.role !== 'owner') return forbidden();
        if (!stepUpValid()) return stepUpNeeded();
        const search = new URL(request.url).searchParams;
        const format = search.get('format') === 'csv' ? 'csv' : 'ndjson';
        const workspace = search.get('workspace');
        const rows = data.activity
          .filter((row) => !workspace || row.lane.startsWith(`${workspace}/`))
          .map((row) => ({ id: row.id, at: row.at, kind: 'trace', tool: row.tool, lane: row.lane, workspace: row.lane.split('/')[0], principal: principalId(row.principal), effect: row.effect, status: row.status, summary: row.summary, run_id: row.run_id ?? null }));
        const columns = ['id', 'at', 'kind', 'tool', 'lane', 'workspace', 'principal', 'effect', 'status', 'summary', 'run_id'] as const;
        const text =
          format === 'csv'
            ? [columns.join(','), ...rows.map((row) => columns.map((column) => csvCell(row[column])).join(','))].join('\n') + '\n'
            : rows.map((row) => JSON.stringify(row)).join('\n') + '\n';
        const stamp = new Date().toISOString().slice(0, 10);
        return new HttpResponse(text, {
          headers: {
            'Content-Type': format === 'csv' ? 'text/csv; charset=utf-8' : 'application/x-ndjson',
            'Content-Disposition': `attachment; filename="openajar-activity-${data.org.slug}-${stamp}.${format}"`,
          },
        });
      }),
    ),
  ];
}
