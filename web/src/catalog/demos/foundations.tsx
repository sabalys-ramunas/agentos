/** Foundations: the tokens (colour, type, space, radius, depth, motion, layers), the kinds of work, icons and marks. */
import { Box, FlaskConical, Globe, Inbox, Radio, Search } from 'lucide-react';
import { AgentMark, BusinessMark, PersonMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { LaneIcon } from '@/components/lane-icon';
import { Card } from '@/components/ui/card';
import { KINDS, KindEyebrow, KindTile } from '@/components/ui/kind';
import type { AnyKind, Kind } from '@/components/ui/kind';
import { Swatch, TokenChip } from '@/components/ui/swatch';
import keys from '../../../tokens/theme-keys.json';
import { Example, Row } from '../parts';

// Whole class names, so Tailwind finds them.
const ROLES = [
  ['text-display', 'Five jobs running on four machines.'],
  ['text-page-title', 'Environments'],
  ['text-page-title-compact', 'Environments, on a phone'],
  ['text-headline', 'Pay €412.50 to Nordic Ropeworks?'],
  ['text-heading', 'What each person gets'],
  ['text-section-title', 'Running now'],
  ['text-lede', 'The computers your agents work on, one per chat.'],
  ['text-card-title', 'Black Friday offer page'],
  ['text-table-header', 'Who and where'],
  ['text-label', 'Run again'],
  ['text-body', 'Everything read: cells and paragraphs.'],
  ['text-secondary', 'A row’s second line, descriptions.'],
  ['text-caption', 'Timestamps, help under a field.'],
  ['text-number', '1,284 · 0:50 · 13.5 MB'],
  ['text-stat', '94%'],
  ['text-code', 'pnpm vitest run'],
  ['text-code-caption', 'feat/black-friday-offer'],
  ['text-eyebrow', 'Tests'],
  ['text-micro', 'CC'],
] as const;

const COLOURS: Array<[string, string[]]> = [
  ['Surfaces', ['surface-page', 'surface-card', 'surface-inset', 'surface-raised', 'surface-inverse']],
  ['Lines', ['line-default', 'line-strong', 'line-control', 'focus-ring']],
  ['Text', ['text-primary', 'text-secondary', 'text-tertiary', 'text-disabled']],
  ['Status', ['status-ok-dot', 'status-warn-dot', 'status-bad-dot', 'status-live-dot', 'status-idle-dot']],
  ['Kinds of work', ['kind-test-mark', 'kind-design-mark', 'kind-build-mark', 'kind-server-mark']],
  ['Terminal and diff', ['terminal-bg', 'ansi-green', 'ansi-red', 'diff-add-fill', 'diff-del-fill']],
  ['Charts and boards', ['chart-bar', 'chart-bar-strong', 'chart-grid', 'board-wire', 'night']],
];

const SPACE = ['1', '2', '3', '4', '5', '6', '8', '10', '12', '16'];
const WIDTH: Record<string, string> = { '1': 'w-1', '2': 'w-2', '3': 'w-3', '4': 'w-4', '5': 'w-5', '6': 'w-6', '8': 'w-8', '10': 'w-10', '12': 'w-12', '16': 'w-16' };
const RADIUS: Record<string, string> = { sm: 'rounded-sm', md: 'rounded-md', lg: 'rounded-lg', xl: 'rounded-xl', full: 'rounded-full' };

export function TokensDemo() {
  return (
    <div className="grid gap-4">
      {COLOURS.map(([group, names]) => (
        <Example key={group} label={group}>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            {names.map((name) => (
              <Swatch key={name} color={`var(--${name})`} name={name} value={`--${name}`} />
            ))}
          </div>
        </Example>
      ))}
      <div className="grid gap-4 md:grid-cols-2">
        <Card className="grid content-start gap-3 p-5">
          <span className="text-card-title">Space: the 4 px grid</span>
          {SPACE.map((step) => (
            <div key={step} className="flex items-center gap-3">
              <TokenChip name={`--space-${step}`} className="w-28 shrink-0" />
              <span className={`h-2 rounded-sm bg-chart-bar-strong ${WIDTH[step]}`} />
            </div>
          ))}
        </Card>
        <Card className="grid content-start gap-4 p-5">
          <span className="text-card-title">Radius, depth, motion and layers</span>
          <Row className="gap-3">
            {keys.radius.filter((r) => RADIUS[r]).map((r) => (
              <span key={r} className={`grid size-14 place-items-center border border-line-strong bg-inset text-caption text-muted ${RADIUS[r]}`}>
                {r}
              </span>
            ))}
          </Row>
          <Row className="gap-3">
            <span className="grid h-14 w-28 place-items-center rounded-xl border border-line bg-card text-caption text-muted shadow-lift">shadow-lift</span>
            <span className="grid h-14 w-28 place-items-center rounded-xl border border-line bg-raised text-caption text-muted shadow-popover">shadow-popover</span>
            <span className="grid h-14 w-28 place-items-center rounded-xl border border-line bg-raised text-caption text-muted shadow-dialog">shadow-dialog</span>
          </Row>
          <Row>
            {Object.values(keys.duration).map((name) => (
              <TokenChip key={name} name={name} />
            ))}
          </Row>
          <Row>
            {Object.entries(keys.z).map(([name, value]) => (
              <TokenChip key={name} name={`z-${value} ${name}`} />
            ))}
          </Row>
        </Card>
      </div>
    </div>
  );
}

export function TypeDemo() {
  return (
    <Card className="divide-y divide-line">
      {ROLES.map(([className, sample]) => (
        <div key={className} className="grid gap-1 px-5 py-3 md:grid-cols-[200px_minmax(0,1fr)] md:items-baseline">
          <TokenChip name={className} className="justify-self-start" />
          <span className={`${className} min-w-0`}>{sample}</span>
        </div>
      ))}
    </Card>
  );
}

const WORK: Kind[] = ['test', 'design', 'build', 'server'];
const NEUTRAL: AnyKind[] = ['machine', 'push', 'post', 'payment', 'vm', 'lane', 'brand', 'email'];

export function KindsDemo() {
  return (
    <Card className="grid gap-4 p-5">
      <Row className="gap-4">
        {WORK.map((kind) => (
          <KindEyebrow key={kind} kind={kind} />
        ))}
      </Row>
      <Row className="gap-3">
        {WORK.map((kind) => (
          <span key={kind} className="flex items-center gap-1">
            <KindTile kind={kind} size="xs" />
            <KindTile kind={kind} size="sm" />
            <KindTile kind={kind} size="md" labelled />
            <KindTile kind={kind} size="lg" />
            <KindTile kind={kind} size="xl" />
            <KindTile kind={kind} size="md" waiting />
          </span>
        ))}
      </Row>
      <Row className="gap-3">
        {NEUTRAL.map((kind) => (
          <span key={kind} className="flex items-center gap-2 text-secondary text-muted">
            <KindTile kind={kind} size="sm" />
            {KINDS[kind].word}
          </span>
        ))}
      </Row>
    </Card>
  );
}

export function IconsDemo() {
  return (
    <Card className="grid gap-4 p-5">
      <Example label="lucide-react, 16 px, stroke 1.5 (set once in CSS)">
        <Row className="gap-4 text-muted">
          <Search className="size-4" />
          <Inbox className="size-4" />
          <Globe className="size-4" />
          <FlaskConical className="size-4" />
          <Box className="size-4" />
          <Radio className="size-4" />
        </Row>
      </Example>
      <Example label="LaneIcon: our own line icons for a browser, a terminal, a service and a phone">
        <Row className="gap-4 text-muted">
          {(['browser', 'terminal', 'service', 'android'] as const).map((kind) => (
            <LaneIcon key={kind} kind={kind} className="size-4" />
          ))}
        </Row>
      </Example>
      <Example label="BusinessMark, PersonMark, AgentMark, SiteMark: fills on small marks only">
        <Row className="gap-3">
          <BusinessMark name="Harbor Supply" color="var(--identity-2)" size={24} />
          <PersonMark id="u-priya" name="Priya Shah" size={24} />
          <PersonMark id="u-tom" name="Tom Berg" size={24} />
          <AgentMark principal={{ kind: 'agent', id: 'claude-code' }} name="Claude Code" size={24} />
          <AgentMark principal={{ kind: 'agent', id: 'codex' }} name="Codex" size={24} />
          <SiteMark site="instagram" size={20} />
          <SiteMark site="linkedin" size={20} />
          <SiteMark site="reddit" size={20} tile />
          <SiteMark site="unknown.example" size={20} tile />
        </Row>
      </Example>
    </Card>
  );
}
