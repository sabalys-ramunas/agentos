/**
 * The mock of Brand (`/brand`, agentos.api.web.brand) and of disposable VMs (`/vms`). Harbor Supply's web shop
 * is a product profile with a waiting draft an agent proposed; Aster Coffee is a business with no draft. As on
 * the node, only the owner reads and decides: anyone else is refused, the proposals for Needs you included.
 */
import { http, HttpResponse } from 'msw';
import type { BrandDraft, BrandProfile, BrandProposal, Vm } from '@/api/types';
import type { Helpers } from './s3';
import type { OrgData } from './seed';

const MINUTE = 60_000;

export interface MockBrand {
  profile: BrandProfile;
  /** What the profile becomes once the waiting draft is approved. */
  proposed?: Partial<BrandProfile>;
}

export function seedBrands(now = Date.now()): MockBrand[] {
  const draft: BrandDraft = {
    version: 4,
    created_at: new Date(now - 38 * MINUTE).toISOString(),
    created_by: 'claude-code',
    reason: 'The blue accent on the dark checkout bar fails contrast (2.1 against 4.5). A deeper teal passes in both themes and keeps the shop calm.',
    diff: [
      'brand.tokens: 1 added (color.accent-strong)',
      'brand.tokens.color.accent changed: #1f5fad -> #0e7c66',
      'guideline one-accent added: One accent colour per screen (advisory)',
    ],
  };
  const shop: BrandProfile = {
    ref: 'harbor/shop',
    workspace: 'harbor',
    profile: 'shop',
    kind: 'product',
    display_name: 'Harbor Supply shop',
    headline: 'Boat parts and chandlery, shipped in a day',
    version: 3,
    state: 'approved',
    draft_waiting: 4,
    website: 'https://shop.harborsupply.example',
    created_at: new Date(now - 9 * 86_400_000).toISOString(),
    decided_at: new Date(now - 9 * 86_400_000 + 3_600_000).toISOString(),
    brand: {
      tokens: [
        { name: 'color.accent', type: 'color', value: '#1F5FAD' },
        { name: 'color.ink', type: 'color', value: '#14181F' },
        { name: 'color.paper', type: 'color', value: '#FFFFFF' },
        { name: 'color.muted', type: 'color', value: '#5B6B80' },
        { name: 'color.danger', type: 'color', value: '#C6373C' },
        { name: 'color.text-on-accent', type: 'color', value: '{color.paper}' },
        { name: 'space.1', type: 'dimension', value: '4px' },
        { name: 'space.2', type: 'dimension', value: '8px' },
        { name: 'space.4', type: 'dimension', value: '16px' },
        { name: 'radius.md', type: 'dimension', value: '8px' },
        { name: 'font.body', type: 'fontFamily', value: 'Inter, system-ui, sans-serif' },
        { name: 'font.size.body', type: 'dimension', value: '16px' },
      ],
      fonts: [
        { family: 'Inter', role: 'body', weights: [400, 500, 600] },
        { family: 'Fraunces', role: 'display', weights: [600] },
      ],
      logo: 'sha256:9f2c1d0e7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e',
      components: [
        { name: 'Button', attr: 'data-slot=button', class_pattern: null },
        { name: 'ProductCard', attr: 'data-component=product-card', class_pattern: null },
        { name: 'PriceTag', attr: null, class_pattern: '^price(-|$)' },
        { name: 'CheckoutButton', attr: 'data-component=checkout-button', class_pattern: null },
        { name: 'SearchField', attr: 'data-slot=search', class_pattern: null },
        { name: 'Badge', attr: 'data-slot=badge', class_pattern: null },
      ],
      theme: { how: 'attr', name: 'data-theme', light: 'light', dark: 'dark' },
      widths: [360, 768, 1280],
      engine: 'chromium',
    },
    guidelines: [
      { id: 'contrast', text: 'Body text is easy to read in both themes', check: 'contrast.text', threshold: 4.5, advisory: false },
      { id: 'tap', text: 'Every control is easy to tap on a phone', check: 'target.size', threshold: 44, advisory: false },
      { id: 'prices', text: 'Prices always show the currency and two decimals', check: null, threshold: null, advisory: true },
    ],
    references: [
      { name: 'Linear', url: 'https://linear.app', note: 'the density we want on the order pages' },
      { name: 'Our print catalogue', url: 'https://harborsupply.example/catalogue-2026.pdf', note: null },
    ],
    environments: [
      { name: 'local', url: 'http://localhost:5173' },
      { name: 'staging', url: 'https://staging.shop.harborsupply.example' },
    ],
    draft,
  };
  const cafe: BrandProfile = {
    ref: 'aster/cafe',
    workspace: 'aster',
    profile: 'cafe',
    kind: 'business',
    display_name: 'Aster Coffee',
    headline: 'Small batch roastery and café',
    version: 2,
    state: 'approved',
    draft_waiting: null,
    website: 'https://astercoffee.example',
    created_at: new Date(now - 40 * 86_400_000).toISOString(),
    decided_at: new Date(now - 40 * 86_400_000).toISOString(),
    brand: {
      tokens: [
        { name: 'color.roast', type: 'color', value: '#A0763F' },
        { name: 'color.cream', type: 'color', value: '#F6EFE4' },
        { name: 'color.ink', type: 'color', value: '#2B2118' },
      ],
      fonts: [{ family: 'Source Serif 4', role: 'heading', weights: [600] }],
      logo: null,
      components: [],
      theme: null,
      widths: [],
      engine: null,
    },
    guidelines: [{ id: 'warm', text: 'Photos are warm and taken in daylight', check: null, threshold: null, advisory: true }],
    references: [],
    environments: [],
    draft: null,
  };
  const proposedTokens = shop.brand!.tokens.map((token) => (token.name === 'color.accent' ? { ...token, value: '#0E7C66' } : token));
  const proposedGuidelines = [...shop.guidelines, { id: 'one-accent', text: 'One accent colour per screen', check: null, threshold: null, advisory: true }];
  shop.draft = { ...draft, tokens: [...proposedTokens, { name: 'color.accent-strong', type: 'color', value: '#0A5C4B' }], guidelines: proposedGuidelines };
  return [
    {
      profile: shop,
      proposed: {
        version: 4,
        brand: { ...shop.brand!, tokens: [...proposedTokens, { name: 'color.accent-strong', type: 'color', value: '#0A5C4B' }] },
        guidelines: proposedGuidelines,
      },
    },
    { profile: cafe },
  ];
}

const row = ({ profile }: MockBrand) => {
  const { ref, workspace, profile: id, kind, display_name, headline, version, state, draft_waiting } = profile;
  return { ref, workspace, profile: id, kind, display_name, headline, version, state, draft_waiting };
};

export function proposalOf({ profile }: MockBrand): BrandProposal | null {
  const draft = profile.draft;
  if (!draft) return null;
  return {
    id: `brand:${profile.ref}@${draft.version}`,
    kind: 'brand',
    ref: profile.ref,
    workspace: profile.workspace,
    display_name: profile.display_name,
    version: draft.version,
    created_at: draft.created_at,
    created_by: draft.created_by,
    reason: draft.reason,
    diff: draft.diff,
  };
}

export function seedVms(now = Date.now()): Vm[] {
  return [
    {
      id: 'vm_7f3a9c21', lane: 'harbor/ci-box', state: 'ready', profile: 'small', server_type: 'cpx21', location: 'hel1',
      created_at: new Date(now - 20 * MINUTE).toISOString(), ready_at: new Date(now - 19 * MINUTE).toISOString(),
      expires_at: new Date(now + 40 * MINUTE).toISOString(), minutes_left: 40, price_hourly_eur: 0.0119, cost_eur_so_far: 0.01,
      purpose: 'Run the shop end-to-end tests on a clean machine', started_by: 'An agent',
    },
    {
      id: 'vm_2c8e4b10', lane: 'meridian/build', state: 'booting', profile: 'large', server_type: 'cpx41', location: 'fsn1',
      created_at: new Date(now - 1 * MINUTE).toISOString(), ready_at: null,
      expires_at: new Date(now + 119 * MINUTE).toISOString(), minutes_left: 119, price_hourly_eur: 0.0389, cost_eur_so_far: 0.04,
      purpose: 'Build the site images', started_by: 'The owner',
    },
  ];
}

export function brandHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const brandsOf = (record: OrgData): MockBrand[] => {
    record.brands ??= record.org.slug === 'northwind-group' ? seedBrands() : [];
    return record.brands;
  };
  const ownerOnly = () => failure(403, 'denied', 'Only the owner signed in to the panel can read or change profiles here.');
  const find = (record: OrgData, ws: unknown, id: unknown) => brandsOf(record).find((item) => item.profile.ref === `${String(ws)}/${String(id)}`);

  const decide = (approve: boolean) =>
    org(async (record, { params, request }) => {
      if (record.role !== 'owner') return ownerOnly();
      const item = find(record, params.ws, params.id);
      if (!item) return failure(404, 'not_found', 'No such profile.');
      const body = (await request.json().catch(() => ({}))) as { version?: unknown };
      if (typeof body.version !== 'number') return failure(400, 'invalid', 'Say which version you read: version is a number.');
      if (!item.profile.draft || item.profile.draft.version !== body.version) return failure(409, 'conflict', 'That draft is not the one waiting any more. Read the profile again.');
      const version = item.profile.draft.version;
      item.profile = approve
        ? { ...item.profile, ...item.proposed, version, state: 'approved', draft: null, draft_waiting: null, decided_at: new Date().toISOString() }
        : { ...item.profile, draft: null, draft_waiting: null };
      delete item.proposed;
      return HttpResponse.json({ ref: item.profile.ref, version, state: approve ? 'approved' : 'rejected' });
    });

  return [
    http.get(
      '/api/orgs/:org/brand',
      org((record, { request }) => {
        if (record.role !== 'owner') return ownerOnly();
        const workspace = new URL(request.url).searchParams.get('workspace');
        return HttpResponse.json({ available: true, profiles: brandsOf(record).filter((item) => !workspace || item.profile.workspace === workspace).map(row) });
      }),
    ),
    http.get(
      '/api/orgs/:org/brand/proposals',
      org((record) => {
        if (record.role !== 'owner') return ownerOnly();
        return HttpResponse.json(brandsOf(record).map(proposalOf).filter((item) => item !== null));
      }),
    ),
    http.get(
      '/api/orgs/:org/brand/:ws/:id',
      org((record, { params }) => {
        if (record.role !== 'owner') return ownerOnly();
        const item = find(record, params.ws, params.id);
        return item ? HttpResponse.json(item.profile) : failure(404, 'not_found', 'No such profile.');
      }),
    ),
    http.post('/api/orgs/:org/brand/:ws/:id/approve', decide(true)),
    http.post('/api/orgs/:org/brand/:ws/:id/reject', decide(false)),
    http.get(
      '/api/orgs/:org/vms',
      org((record) => {
        if (record.role !== 'owner') return failure(403, 'denied', 'Only the owner sees the cloud machines this computer rents.');
        record.vms ??= record.org.slug === 'northwind-group' ? seedVms() : [];
        return HttpResponse.json(record.org.slug === 'northwind-group'
          ? { available: true, vms: record.vms }
          : { available: false, reason: 'This computer rents no cloud machines: no VM provider is set up on it.', vms: [] });
      }),
    ),
  ];
}
