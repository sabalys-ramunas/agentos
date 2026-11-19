import { Link } from '@tanstack/react-router';
import { GitBranch, Globe, Megaphone, SquareTerminal } from 'lucide-react';
import type { ReactNode } from 'react';
import { Logo } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import { ApprovalPicture, PhoneFrame } from '../onboarding/pictures';

/** The jobs people come for, around the picture: work on a computer of the agent's own first, then the features. */
const JOBS: { text: string; icon: typeof Globe; className: string }[] = [
  { text: 'Fix the failing checkout test', icon: SquareTerminal, className: 'top-10 left-8' },
  { text: 'Build the pricing page, check on a phone', icon: Globe, className: 'top-24 right-8' },
  { text: 'Upgrade to React 19, push a branch', icon: GitBranch, className: 'bottom-24 left-8' },
  { text: 'Post for Aster Coffee every Tuesday', icon: Megaphone, className: 'bottom-10 right-8' },
];

/** The promise in one picture: the work gets done on the agent's own computer, and the few questions reach your phone. */
export function PromisePicture() {
  return (
    <div
      aria-hidden="true"
      className="relative hidden min-h-dvh items-center justify-center overflow-hidden border-l border-line bg-inset bg-[radial-gradient(var(--border)_1px,transparent_1px)] [background-size:16px_16px] lg:flex"
    >
      {JOBS.map((job) => (
        <span key={job.text} className={cn('absolute inline-flex items-center gap-2 rounded-full border border-line bg-card px-3 py-2 text-secondary shadow-card', job.className)}>
          <job.icon className="size-4 text-faint" />
          {job.text}
        </span>
      ))}
      <PhoneFrame>
        <div className="mb-3 flex items-center justify-between text-micro text-faint font-normal">
          <Logo className="size-4" />
          12 done on their own · 1 needs you
        </div>
        <ApprovalPicture business="Aster Coffee" small className="border-0 p-0 shadow-none" />
        <p className="mt-3 text-micro text-faint font-normal">Agents work on computers of their own. A post set to ask, a payment or a push to main asks you first.</p>
      </PhoneFrame>
    </div>
  );
}

interface AuthLayoutProps {
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  /** A picture beside the form on a wide screen: the promise, or what you will be asked. */
  aside?: ReactNode;
}

export function AuthLayout({ title, description, children, footer, aside }: AuthLayoutProps) {
  const form = (
    <div className="w-full max-w-[400px]">
      <Link to="/" aria-label="agentos" className="mb-10 inline-flex items-center gap-2 rounded-md text-section-title">
        <Logo className="size-7" />
        agentos
      </Link>
      <h1 className="text-headline text-balance">{title}</h1>
      {description ? <div className="mt-3 text-lede text-muted">{description}</div> : null}
      {children ? <div className="mt-8">{children}</div> : null}
      {footer ? <p className="mt-10 text-secondary text-faint">{footer}</p> : null}
    </div>
  );
  if (!aside) {
    return <main className="flex min-h-dvh flex-col items-center px-4 pt-[12vh] pb-8">{form}</main>;
  }
  return (
    <main className="grid min-h-dvh lg:grid-cols-2">
      <div className="flex flex-col items-center px-4 pt-[12vh] pb-8 lg:justify-center lg:pt-8">{form}</div>
      {aside}
    </main>
  );
}

/** A short message above a form, for what happened before this page. */
export function Notice({ children, alert }: { children: ReactNode; alert?: boolean }) {
  return (
    <p role={alert ? 'alert' : 'status'} className={cn('mb-6 rounded-lg border border-line bg-inset px-4 py-3 text-body text-muted')}>
      {children}
    </p>
  );
}

/** "or", between the buttons and the email field. */
export function Or() {
  return (
    <div className="my-4 flex items-center gap-3 text-secondary text-faint" aria-hidden="true">
      <span className="h-px flex-1 bg-line" />
      or
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}
