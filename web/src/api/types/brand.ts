import type { StudioFinding } from './studio';

/** Component use and look-alikes of a profile, from its newest stored system check. */
export interface BrandComponents {
  profile: string;
  /** The run they come from; null when no system check of this profile is stored. */
  run: string | null;
  url: string | null;
  checked_at: string | null;
  /** Elements per known component, counted on the run's screens. */
  components: { name: string; uses: number; screens: number; findings: number }[];
  /** Controls drawn outside every known component. */
  hand_rolled: number;
  /** Values that only look like a token (the provenance probe). Null: not measured. */
  look_alikes: { token: number; derived: number; look_alike: number } | null;
  look_alike_findings: StudioFinding[];
  on_tokens: Record<string, number>;
}

// ---- Brand: product and business profiles (GET /api/orgs/{org}/brand/...) ----

export type BrandKind = 'product' | 'business';

export interface BrandProfileRow {
  /** `workspace/profile`. */
  ref: string;
  workspace: string;
  profile: string;
  kind: BrandKind;
  display_name: string;
  headline: string | null;
  version: number;
  state: 'draft' | 'approved' | 'rejected' | 'superseded';
  /** The version of a draft an agent proposed, waiting for the owner. */
  draft_waiting: number | null;
}

export interface BrandProfiles {
  available: boolean;
  profiles: BrandProfileRow[];
}

export interface BrandToken {
  /** The token's path: `color.brand.primary`. */
  name: string;
  type: string | null;
  /** The value as text: a hex colour, `16px`, or an alias such as `{color.ink}`. */
  value: string;
}

export interface BrandSection {
  tokens: BrandToken[];
  fonts: { family: string; role: string | null; weights: number[] }[];
  logo: string | null;
  components: { name: string; attr: string | null; class_pattern: string | null }[];
  theme: { how: string; name: string | null; light: string | null; dark: string | null } | null;
  widths: number[];
  engine: string | null;
}

export interface BrandGuideline {
  id: string;
  text: string;
  check: string | null;
  threshold: number | null;
  advisory: boolean;
}

export interface BrandReference {
  name: string;
  url: string;
  note: string | null;
}

export interface BrandDraft {
  version: number;
  created_at: string;
  /** Who proposed it, as the node keeps it (an agent's name). */
  created_by: string | null;
  /** Why the agent proposes it, in its own words: data. */
  reason: string | null;
  /** What approving it changes, one plain line each. */
  diff: string[];
  /** The draft's own tokens, flattened as the profile's are, so each change is shown beside today's value. */
  tokens?: BrandToken[];
  /** The draft's guidelines. */
  guidelines?: BrandGuideline[];
}

export interface BrandProfile extends BrandProfileRow {
  website: string | null;
  created_at: string;
  decided_at: string | null;
  brand: BrandSection | null;
  guidelines: BrandGuideline[];
  references: BrandReference[];
  environments: { name: string; url: string }[];
  draft: BrandDraft | null;
}

/**
 * A brand proposal for Needs you, beside approvals: decided with approve or reject on the
 * profile (owner in person), never through the approval routes.
 */
export interface BrandProposal {
  /** `brand:workspace/profile@version`: unique beside approval ids. */
  id: string;
  kind: 'brand';
  ref: string;
  workspace: string;
  display_name: string;
  version: number;
  created_at: string;
  created_by: string | null;
  reason: string | null;
  diff: string[];
}

export interface BrandDecision {
  ref: string;
  version: number;
  state: 'approved' | 'rejected';
}
