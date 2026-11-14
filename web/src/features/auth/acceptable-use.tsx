import { Link } from '@tanstack/react-router';
import type { ReactNode } from 'react';
import { Logo } from '@/components/ui/misc';

function Rule({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="text-16 font-semibold">{title}</h2>
      <div className="mt-2 grid gap-2 text-14 text-muted">{children}</div>
    </section>
  );
}

/** Plain, short and public. The wording is a draft until legal has read it, and says so. */
export function AcceptableUse() {
  return (
    <main className="mx-auto w-full max-w-xl px-4 pt-10 pb-16 sm:pt-16">
      <Link to="/" aria-label="agentos home" className="inline-flex rounded-md">
        <Logo className="size-8" />
      </Link>
      <h1 className="mt-8 text-24 font-semibold">Acceptable use policy</h1>
      <p className="mt-2 text-13 text-faint">Version 2026-11</p>
      <p role="note" className="mt-6 rounded-lg border border-line bg-subtle px-4 py-3 text-14 text-muted">
        Draft wording, awaiting legal review. It says what we mean today, and the final text may change.
      </p>
      <p className="mt-6 text-14 text-muted">
        agentos gives your agents a computer on machines you control: browsers, terminals and the accounts you connect. That is a lot of reach, so these are the rules for using it.
        Accepting this policy is how you agree to them.
      </p>
      <Rule title="Do not create accounts in bulk">
        <p>
          Use agentos with accounts you already own or are allowed to run. Do not use it to sign up for accounts at scale, on any
          platform. We limit this, and we look for it.
        </p>
      </Rule>
      <Rule title="Do not send spam">
        <p>No unsolicited bulk messages, comments or posts, and no fake engagement such as bought likes, follows or reviews.</p>
      </Rule>
      <Rule title="Do not deceive or harm people">
        <p>
          No impersonation, no phishing, no fraud, no harassment, and nothing illegal. No collecting someone else's passwords or
          personal data without the right to.
        </p>
      </Rule>
      <Rule title="Follow the rules of the sites you use">
        <p>
          You are responsible for what your agents do on your accounts, including each site's own terms. agentos does not change
          what those sites allow.
        </p>
      </Rule>
      <Rule title="Do not attack the service">
        <p>No probing or overloading agentos or anyone else, and no working around plan limits or holds.</p>
      </Rule>
      <Rule title="What happens if these are broken">
        <p>
          We may put a hold on an org, limit it, or suspend it. Signals that point to bulk account creation or spam go to a person
          for review before a suspension stands. Tell us about abuse at abuse@agentos.example.
        </p>
      </Rule>
    </main>
  );
}
