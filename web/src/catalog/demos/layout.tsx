/** Layout and shell: the page header, sections, cards and the shell's own switch. */
import { Play } from 'lucide-react';
import { TechnicalToggle } from '@/components/shell/technical-toggle';
import { Button } from '@/components/ui/button';
import { Card, CardFooter, CardHeader, cardLink, FormCard, Section } from '@/components/ui/card';
import { SiteMark } from '@/components/brand/site-mark';
import { KindEyebrow, KindTile } from '@/components/ui/kind';
import { PageHeader } from '@/components/ui/misc';
import { StateBadge } from '@/components/ui/state-badge';
import { Status } from '@/components/ui/status';
import { Example } from '../parts';

export function PageHeaderDemo() {
  return (
    <Card className="p-5">
      <PageHeader
        eyebrow={<KindEyebrow kind="server" label="Environments" />}
        title="Black Friday offer page"
        description="One sentence that says what this page is for, in the secondary ink, under the title."
        actions={<Button variant="primary" icon={<Play className="size-4" />}>Run it</Button>}
      />
      <PageHeader
        lead={<KindTile kind="test" size="xl" />}
        eyebrow={<KindEyebrow kind="test" tile={false} />}
        title="Tests in harbor-shop"
        aside={<Status chip status={{ label: 'Running', tone: 'live' }} />}
        description={<span className="font-mono text-code">$ openajar run -- pnpm vitest run</span>}
        actions={<Button>Copy follow command</Button>}
      />
      <PageHeader
        className="mb-0"
        lead={<SiteMark site="instagram" size={40} tile label={false} />}
        title="Aster Coffee · Instagram"
        aside={<StateBadge chip state="working" />}
        description="Sealed account · Runs on Mac mini (home) · Last used just now"
        actions={<Button>Put to sleep</Button>}
      />
    </Card>
  );
}

export function CardsDemo() {
  return (
    <div className="grid gap-4">
      <div className="grid gap-4 md:grid-cols-3">
        {(['test', 'design', 'build'] as const).map((kind) => (
          <Card key={kind} kind={kind} className="grid content-start gap-2 p-4">
            <KindEyebrow kind={kind} />
            <h3 className="text-section-title">{{ test: 'Cart totals round per line', design: 'Check the product page', build: 'Fresh install and build' }[kind]}</h3>
            <p className="text-secondary text-muted">A job card takes its kind as a 2 px top edge.</p>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader title="Needs you" meta="the oldest for 2 h" action={<Button variant="ghost" size="sm">See all 6</Button>} />
        <p className="px-5 py-4 text-body text-muted">A card’s head: title, quiet meta, the action, one hairline. No grey band.</p>
        <CardFooter>Updated 14:09</CardFooter>
      </Card>
      <div className="grid gap-4 md:grid-cols-2">
        <Example label="cardLink: a whole card that is one link">
          <a href="#cards" className={`${cardLink} grid gap-1 p-5`}>
            <span className="text-card-title">Prepared machines</span>
            <span className="text-secondary text-muted">2 ready · warm pool off · €4.20 this month</span>
          </a>
        </Example>
        <Example label="Section: a titled block of a page">
          <Section title="Last hour" description="Real runs only, start to end." action={<Button size="sm">All runs</Button>}>
            <Card className="p-5 text-body text-muted">The section’s content.</Card>
          </Section>
        </Example>
      </div>
      <FormCard title="A settings card" description="Content on top, a well footer with a note and the action." footnote="Saved for everyone in Northwind." action={<Button variant="primary">Save</Button>}>
        <TechnicalToggle variant="row" />
      </FormCard>
    </div>
  );
}
