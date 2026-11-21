/** Dawn's own parts: the wrapper tile, the picture of an environment's screen, and the choice cards. */
import { CreditCard, GitBranch, KeyRound, Mail, MessageSquare, Server, Workflow } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useState } from 'react';
import { PictureChip, ScreenPicture } from '@/components/screen-picture';
import { Card } from '@/components/ui/card';
import { Choice, ChoiceGroup } from '@/components/ui/choice';
import { StatusDot } from '@/components/ui/status';
import { AppTile } from '@/components/ui/tile';
import type { TileTone } from '@/components/ui/tile';
import { Row } from '../parts';

const TONES: [TileTone, LucideIcon][] = [
  ['violet', GitBranch],
  ['blue', KeyRound],
  ['pink', MessageSquare],
  ['amber', CreditCard],
  ['green', Mail],
  ['ink', Server],
  ['teal', Workflow],
];

export function TilesDemo() {
  return (
    <Card className="grid gap-4 p-5">
      <Row className="gap-3">
        {TONES.map(([tone, Icon]) => (
          <AppTile key={tone} tone={tone} icon={Icon} label={tone} />
        ))}
      </Row>
      <Row className="gap-3">
        <AppTile tone="violet" icon={GitBranch} size="sm" />
        <AppTile tone="violet" icon={GitBranch} />
        <AppTile tone="violet" icon={GitBranch} size="lg" />
      </Row>
    </Card>
  );
}

export function ScreenPictureDemo() {
  return (
    <div data-studio="ignore" className="grid gap-4 sm:grid-cols-3">
      <ScreenPicture state="starting" title="Fix the flaky checkout test">
        <span className="absolute top-2 left-2">
          <PictureChip>
            <StatusDot tone="live" starting />
            Starting
          </PictureChip>
        </span>
      </ScreenPicture>
      <ScreenPicture state="awake" title="Invoice export to CSV">
        <span className="absolute top-2 left-2">
          <PictureChip>
            <StatusDot tone="idle" />
            Idle
          </PictureChip>
        </span>
      </ScreenPicture>
      <ScreenPicture state="asleep" title="Release notes page">
        <span className="absolute top-2 left-2">
          <PictureChip>
            <StatusDot tone="neutral" />
            Closed
          </PictureChip>
        </span>
      </ScreenPicture>
    </div>
  );
}

export function ChoicesDemo() {
  const [value, setValue] = useState<'save' | 'none'>('save');
  return (
    <Card className="p-5">
      <ChoiceGroup label="When the agent is done" showLabel value={value} onValueChange={setValue} columns={2}>
        <Choice value="save" title="Save it when idle" description="Its machine is saved and comes back in the next chat." badge="Recommended" />
        <Choice value="none" title="Single use" description="Deleted once the work is pushed." />
      </ChoiceGroup>
    </Card>
  );
}
