import { Hammer } from 'lucide-react';
import type { ReactNode } from 'react';
import { Card } from '../ui/card';
import { PageHeader } from '../ui/misc';
import { EmptyState } from '../ui/states';

/**
 * A place whose screen is not built yet. It says so plainly, and what will be here, instead of
 * showing a page with nothing on it. Each new place starts with this; its builder replaces it.
 */
export function BeingBuilt({ title, lede, what, children }: { title: string; lede: string; what: string; children?: ReactNode }) {
  return (
    <>
      <PageHeader title={title} description={lede} />
      <Card>
        <EmptyState icon={<Hammer />} title="This screen is being built" description={what} />
        {children}
      </Card>
    </>
  );
}
