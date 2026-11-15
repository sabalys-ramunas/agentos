/**
 * The mock of what a computer keeps for a person: the vault's lock, browser devices, proxies, sign-ins
 * and the forms a secret is typed into. A form's page is the computer's own (served at /api/forms/...),
 * so the mock tracks only how far each trip got, and applies what it would change once it is done.
 * Nothing here takes a password, a passphrase or a key: the mock refuses such a field the same way.
 */
import { http, HttpResponse } from 'msw';
import type { BrowserDevice, FormKind, FormTrip, LaneLogin, Proxy, VaultEntry } from '@/api/types';
import type { Helpers } from './s3';
import type { AccessData, OrgData } from './seed';

const START_KINDS: FormKind[] = ['vault_unlock', 'vault_entry', 'proxy', 'workspace_export', 'workspace_import'];
const FORM_WAIT_MS = 2_500;
const LOGIN_OVER = ['signed_in', 'cancelled', 'done'];

/** What a fresh org starts with. The full demo has a few devices and proxies; an empty org has none. */
export function seedAccess(empty: boolean): AccessData {
  if (empty) return { vaultState: 'unsealed', devices: [], proxies: [], logins: {}, trips: {} };
  const proxies: Proxy[] = [
    { label: 'de1', host: '198.51.100.21', port: 8080 },
    { label: 'lt1', host: '198.51.100.44', port: 8080 },
  ];
  const devices: BrowserDevice[] = [
    {
      name: 'aster-desk', proxy: 'de1', created_at: '2026-10-02T09:00:00Z', from_lane: 'aster/instagram',
      note: 'The shop accounts', pinned: true,
      pin: { os: 'linux', screen: '1920x1080', window: '1920x1050', gpu: null, timezone: 'Europe/Vilnius', locale: 'en-US', firefox: 158, pinned_at: '2026-10-02T09:01:00Z', pinned_exit_ip: null, pinned_country: 'DE' },
      lanes: [{ id: 'aster/instagram', state: 'warm', proxy: 'de1', pin_copy: true }],
      workspaces: ['aster'],
    },
    {
      name: 'aster-travel', proxy: 'lt1', created_at: '2026-10-20T14:30:00Z', from_lane: null,
      note: null, pinned: false, pin: null, lanes: [], workspaces: [],
    },
  ];
  return { vaultState: 'unsealed', devices, proxies, logins: {}, trips: {} };
}

function publicTrip(trip: FormTrip & { fields?: unknown }): FormTrip {
  const { id, kind, stage, subject, form_url, expires_at, outcome } = trip;
  return { id, kind, stage, subject, form_url, expires_at, ...(outcome ? { outcome } : {}) };
}

/** A trip is done after the person has had time to type: the mock applies what the node would change. */
function settle(data: OrgData, trip: AccessData['trips'][string]) {
  if (trip.stage !== 'waiting' || Date.now() - trip.opened < FORM_WAIT_MS) return;
  trip.stage = 'done';
  trip.form_url = null;
  if (trip.kind === 'vault_unlock') data.vaultState = 'unsealed';
  if (trip.kind === 'vault_entry') {
    const entry: VaultEntry = {
      id: `v_${trip.id.slice(3, 9)}`, name: trip.fields.name ?? 'entry', kind: (trip.fields.kind as VaultEntry['kind']) ?? 'password',
      domain: trip.fields.domain || null, lanes: (trip.fields.lanes ?? '').split(',').filter(Boolean),
      created_at: new Date().toISOString(), last_used: null,
    };
    data.vault = [...data.vault, entry];
  }
  if (trip.kind === 'proxy') {
    data.proxies = [...data.proxies, { label: trip.fields.label ?? 'proxy', host: trip.fields.host ?? '', port: Number(trip.fields.port) || 8080 }];
  }
  if (trip.kind === 'repo_token') {
    // The token was pasted on the computer's page and GitHub took it: the card closes, as the node approves it.
    data.questions = data.questions.filter((item) => item.id !== trip.fields.card);
    trip.outcome = { repo: trip.fields.repo, token_expires_at: new Date(Date.now() + 30 * 86_400_000).toISOString() };
  }
  if (trip.kind === 'login') {
    const lane = trip.subject ?? '';
    const login = data.logins[lane];
    if (login && login.state === 'ask') data.logins[lane] = { ...login, state: 'signed_in', can_save: true, step: null };
  }
}

/** A sign-in the owner has typed into moves on, as the node reports it. */
function progress(data: OrgData, lane: string) {
  const login = data.logins[lane];
  if (!login || login.state !== 'ask') return;
  const opened = Object.values(data.trips).find((trip) => trip.kind === 'login' && trip.subject === lane && trip.stage === 'waiting');
  if (opened && Date.now() - opened.opened > FORM_WAIT_MS) settle(data, opened);
}

function login(data: OrgData, lane: string): LaneLogin {
  progress(data, lane);
  return data.logins[lane] ?? { lane, state: 'none' };
}

function tokenOf(): string {
  return Math.random().toString(36).slice(2, 12) + Math.random().toString(36).slice(2, 12);
}

export function accessHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const trip = (data: OrgData, kind: FormKind, subject: string | null, fields: Record<string, string>) => {
    const id = `ft_${Math.random().toString(36).slice(2, 10)}`;
    const token = tokenOf();
    const made: AccessData['trips'][string] = {
      id, kind, stage: 'waiting', subject, form_url: `/api/forms/${token}`, expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      fields, opened: Date.now(),
    };
    data.trips[id] = made;
    return made;
  };

  return [
    http.get('/api/orgs/:org/vault/status', org((data) => HttpResponse.json({
      initialized: true, state: data.vaultState, mode: 'keyfile', key_version: 1, entries: data.vault.length,
    }))),
    http.post('/api/orgs/:org/vault/seal', org((data) => {
      data.vaultState = 'sealed';
      return HttpResponse.json({ initialized: true, state: 'sealed', mode: 'keyfile', key_version: 1, entries: data.vault.length });
    })),
    http.delete('/api/orgs/:org/vault/:id', org((data, { params }) => {
      const id = String(params.id);
      if (!data.vault.some((entry) => entry.id === id)) return failure(404, 'not_found', 'No vault entry with that name.');
      data.vault = data.vault.filter((entry) => entry.id !== id);
      return HttpResponse.json({ ok: true });
    })),

    http.post('/api/orgs/:org/forms', org(async (data, { request }) => {
      const body = (await request.clone().json()) as { kind?: string; fields?: Record<string, string> };
      if (!START_KINDS.includes(body.kind as FormKind)) return failure(400, 'invalid', 'Ask for a form this computer has.');
      const fields = body.fields ?? {};
      const needs: Record<string, string[]> = { vault_entry: ['name', 'kind', 'domain', 'lanes'], proxy: ['label', 'host', 'port'], workspace_export: ['workspace'] };
      const missing = (needs[body.kind as string] ?? []).find((name) => !fields[name]);
      if (missing) return failure(400, 'invalid', `Give the ${missing} this form is for.`);
      const made = trip(data, body.kind as FormKind, fields.workspace ?? fields.label ?? null, fields);
      return HttpResponse.json(publicTrip(made));
    })),
    http.get('/api/orgs/:org/forms/trips/:id', org((data, { params }) => {
      const found = data.trips[String(params.id)];
      if (!found) return failure(404, 'not_found', 'That form has ended. Start it again.');
      settle(data, found);
      return HttpResponse.json(publicTrip(found));
    })),

    http.get('/api/orgs/:org/browser-devices', org((data) => HttpResponse.json(data.devices))),
    http.post('/api/orgs/:org/browser-devices', org(async (data, { request }) => {
      const body = (await request.clone().json()) as { name: string; proxy_label?: string | null; note?: string | null };
      if (data.devices.some((device) => device.name === body.name)) return failure(409, 'invalid', `A device named ${body.name} already exists.`);
      if (body.proxy_label && !data.proxies.some((proxy) => proxy.label === body.proxy_label)) {
        return failure(400, 'invalid', `No proxy called ${body.proxy_label}.`);
      }
      const made: BrowserDevice = {
        name: body.name, proxy: body.proxy_label ?? null, created_at: new Date().toISOString(), from_lane: null,
        note: body.note ?? null, pinned: false, pin: null, lanes: [], workspaces: [],
      };
      data.devices = [...data.devices, made];
      return HttpResponse.json(made, { status: 201 });
    })),
    http.delete('/api/orgs/:org/browser-devices/:name', org((data, { params }) => {
      const name = String(params.name);
      const device = data.devices.find((item) => item.name === name);
      if (!device) return failure(404, 'not_found', `No device ${name}.`);
      if (device.lanes.length) return failure(409, 'invalid', `Device ${name} is used by lanes. Move them first.`);
      data.devices = data.devices.filter((item) => item.name !== name);
      return HttpResponse.json({ removed: name, trash: null });
    })),
    http.get('/api/orgs/:org/lanes/:ws/:name/device-move', org((data, { params, request }) => {
      const lane = `${params.ws}/${params.name}`;
      const to = new URL(request.url).searchParams.get('to') ?? 'none';
      const target = to === 'none' ? null : data.devices.find((item) => item.name === to) ?? null;
      const current = data.devices.find((item) => item.lanes.some((row) => row.id === lane)) ?? null;
      const changes = [`${lane} moves to ${target ? target.name : 'a device of its own'}.`];
      const warnings = current?.pinned && target?.name !== current.name ? [`Its site has seen ${current.name}, so the site may ask to check the account.`] : [];
      return HttpResponse.json({
        lane, from: current?.name ?? null, to: target?.name ?? null, changes, warnings,
        confirm: warnings.length > 0, proxy: { from: current?.proxy ?? null, to: target?.proxy ?? null },
      });
    })),
    http.post('/api/orgs/:org/lanes/:ws/:name/device', org(async (data, { params, request }) => {
      const lane = `${params.ws}/${params.name}`;
      const body = (await request.clone().json()) as { device: string | null; confirm?: boolean };
      const target = body.device ? data.devices.find((item) => item.name === body.device) : null;
      if (body.device && !target) return failure(404, 'not_found', `No device ${body.device}.`);
      data.devices = data.devices.map((item) => ({ ...item, lanes: item.lanes.filter((row) => row.id !== lane) }));
      if (target) target.lanes = [...target.lanes, { id: lane, state: 'closed', proxy: target.proxy, pin_copy: false }];
      return HttpResponse.json({
        plan: { lane, from: null, to: body.device, changes: [`${lane} moved.`], warnings: [], confirm: false, proxy: { from: null, to: target?.proxy ?? null }, done: true },
      });
    })),

    http.get('/api/orgs/:org/proxies', org((data) => HttpResponse.json(data.proxies))),
    http.post('/api/orgs/:org/proxies', org(async (data, { request }) => {
      const body = (await request.clone().json()) as { label: string; host: string; port: number };
      if (data.proxies.some((proxy) => proxy.label === body.label)) return failure(409, 'invalid', `Proxy ${body.label} already exists.`);
      const made: Proxy = { label: body.label, host: body.host, port: body.port };
      data.proxies = [...data.proxies, made];
      return HttpResponse.json(made, { status: 201 });
    })),

    http.get('/api/orgs/:org/lanes/:ws/:name/login', org((data, { params }) => HttpResponse.json(login(data, `${params.ws}/${params.name}`)))),
    http.post('/api/orgs/:org/lanes/:ws/:name/login/form', org((data, { params }) => {
      const lane = `${params.ws}/${params.name}`;
      const current = data.logins[lane];
      if (!current || LOGIN_OVER.includes(current.state)) {
        data.logins[lane] = {
          id: `la_${lane.slice(-4)}`, lane, state: 'ask', site: 'instagram', site_name: 'Instagram', host: 'instagram.com',
          step: {
            id: 's1', kind: 'sign_in', heading: 'Log in to Instagram', message: '', number: null,
            fields: [
              { name: 'username', label: 'Phone number, username or email', secret: false, length: null, autocomplete: 'username' },
              { name: 'password', label: 'Password', secret: true, length: null, autocomplete: 'current-password' },
            ],
            choices: [], submit: 'Log in', reason: null,
          },
          note: '', error: null, version: 1, can_save: false, saved: null, by: 'owner',
        };
      }
      const made = trip(data, 'login', lane, {});
      return HttpResponse.json({ ...publicTrip(made), login: data.logins[lane] });
    })),
    http.post('/api/orgs/:org/lanes/:ws/:name/login/vault', org((data, { params }) => {
      const lane = `${params.ws}/${params.name}`;
      const entry = data.vault.find((item) => item.kind === 'password' && item.lanes.includes(lane));
      if (!entry) return failure(409, 'vault_no_entry', 'No saved login is bound to this account.');
      data.logins[lane] = { lane, state: 'signed_in', site: 'instagram', site_name: 'Instagram', host: 'instagram.com', step: null, note: `Signed in from ${entry.name}.`, error: null, version: 1, can_save: false, saved: entry.name, by: 'owner' };
      return HttpResponse.json(data.logins[lane]);
    })),
    ...(['refresh', 'take', 'resume', 'cancel'] as const).map((action) =>
      http.post(`/api/orgs/:org/lanes/:ws/:name/login/${action}`, org((data, { params }) => {
        const lane = `${params.ws}/${params.name}`;
        const current = data.logins[lane];
        if (!current) return failure(404, 'login_closed', 'There is no sign-in running on this lane.');
        const next = action === 'take' ? 'paused' : action === 'resume' ? 'ask' : action === 'cancel' ? 'cancelled' : current.state;
        data.logins[lane] = { ...current, state: next as LaneLogin['state'] };
        return HttpResponse.json(data.logins[lane]);
      })),
    ),
  ];
}
