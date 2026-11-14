import { Link } from '@tanstack/react-router';
import { EmptyState } from './ui/states';
import { Button } from './ui/button';

export function NotFound() {
  return (
    <EmptyState
      title="This page does not exist"
      description="It may have moved, or you may not have access to it."
      action={
        <Button asChild>
          <Link to="/">Go home</Link>
        </Button>
      }
    />
  );
}
