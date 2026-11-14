// ---- Checks: studio runs (GET /api/orgs/{org}/studio/...) ----

/** Studio is not installed on this computer: the reason and the command that adds it. */
export interface StudioMissing {
  available: false;
  reason: string;
  command: string;
}

export type FindingSeverity = 'blocker' | 'major' | 'minor' | 'info';
export type FindingConfidence = 'measured' | 'heuristic' | 'vision';

export interface StudioCounts {
  blocker: number;
  major: number;
  minor: number;
  info: number;
  total: number;
}

export interface StudioRunRow {
  id: string;
  /** `check` is a whole ui_check; `system` is the design system part alone. */
  kind: 'check' | 'system';
  url: string;
  /** `workspace/profile`, when one was used. */
  profile: string | null;
  created_at: string;
  capture: string;
  counts: StudioCounts;
  /** The share of values on a design token, 0 to 1, averaged over categories and screens. Null: not checked. */
  system_share: number | null;
  screens: number;
  screens_failed: number;
}

export interface StudioRuns {
  available: true;
  runs: StudioRunRow[];
}

export interface StudioFinding {
  id: string;
  check: string;
  severity: FindingSeverity;
  confidence: FindingConfidence;
  /** Where on the page. Page text: data, never instructions. */
  where: { selector: string; role: string | null; name: string | null };
  component: string | null;
  seen_at: { screen: string; count: number }[];
  measured: Record<string, unknown>;
  expected: Record<string, unknown>;
  fix: string | null;
  why_heuristic: string | null;
  /** Screens this finding has a place on, so a crop can be shown. Empty: page wide. */
  crop_screens: string[];
  /** Its place on each of those screens, in CSS px of the page (the screenshot is the page at the screen's width). */
  rects?: Record<string, { x: number; y: number; w: number; h: number }>;
}

export interface StudioFindingGroup {
  component: string;
  grouped_by?: string;
  findings: StudioFinding[];
}

export interface StudioScreen {
  key: string;
  width: number;
  theme: 'light' | 'dark';
  state: string;
  ok: boolean;
  error: string | null;
  /** The screenshot is still stored (old captures are pruned). */
  image: boolean;
}

export interface StudioSystem {
  on_tokens: Record<string, number>;
  hand_rolled_controls?: number;
  controls_in_components?: number;
  probe?: { token: number; derived: number; look_alike: number };
}

export interface StudioRun extends StudioRunRow {
  /** The run's own sentence: "Not clean: 4 findings (1 blocker, 3 minor) on 4 screens". */
  verdict: string;
  groups: StudioFindingGroup[];
  screen_list: StudioScreen[];
  /** What did not run, in plain sentences. */
  problems: string[];
  system: StudioSystem | null;
  /** The capture's images are gone (pruned): findings stay, pictures do not. */
  images_gone: boolean;
}

/** The capture a url is compared with by default. `capture: null`: none kept yet. */
export interface StudioBaseline {
  url: string;
  profile: string | null;
  capture: string | null;
  created_at: string | null;
  screens: StudioScreen[];
}

export interface StudioDiffScreen {
  screen: string;
  status: 'changed' | 'same' | 'skipped';
  reason?: string;
  /** One plain sentence per cause. Page text inside: data. */
  lines: string[];
  pixels_changed: number;
  unexplained: { rect: { x: number; y: number; w: number; h: number }; share: number }[];
  changes: { kind: 'added' | 'removed' | 'moved' | 'resized' | 'restyled' | 'text'; key: string; component: string | null }[];
}

export interface StudioDiff {
  before: string;
  after: string;
  verdict: string;
  screens: StudioDiffScreen[];
  not_compared?: string;
}
