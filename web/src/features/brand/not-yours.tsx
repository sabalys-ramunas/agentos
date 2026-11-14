import { Lock } from 'lucide-react';
import { ApiError } from '@/api/http';
import { EmptyState } from '@/components/ui/states';

/** The node refuses brand to anyone but the owner in person. */
export function isRefused(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.code === 'denied');
}

/** For anyone who is not the owner: calm, not an error. */
export function BrandNotYours() {
  return (
    <EmptyState
      icon={<Lock />}
      title="Brand is the owner's to see"
      description="A product's colours, type and guidelines are part of who the business is, so only the owner reads and approves them here. Agents use the approved version when they work."
    />
  );
}
