/**
 * Step 2: where should it work? (startup.md, `#s2`). The main path explains a computer in
 * pictures and plain words. The Mac app and a computer we run are shown as what they are, coming
 * soon. What works today, the install command, is folded under "I'm technical", and the computer
 * still turns green here by itself the moment it says hello.
 */
import { ChevronDown, Cloud, Laptop, Server } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNodes } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import { Badge } from '@/components/ui/badge';
import { Choice as ChoiceCard, ChoiceGroup } from '@/components/ui/choice';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { CodeBlock } from '@/components/ui/misc';
import { timeUntil } from '@/lib/format';
import { useAddNode, useRunsAlone } from '../nodes/add-node';
import type { StepContext } from './context';
import { StepFooter } from './context';
import { Ready, SetupLayout } from './layout';
import { ComputerPicture, DoneRow, DownloadPicture, GreenPicture, PictureStep, TerminalPicture } from './pictures';

type Choice = 'mac' | 'cloud' | 'own';

const OPTIONS: { key: Choice; title: string; body: string; badge: string; icon: typeof Laptop; soon?: boolean }[] = [
  {
    key: 'mac',
    title: 'This Mac',
    body: 'Download a small app. Websites see your home internet, so your logins stay trusted. Works while this Mac is on.',
    badge: 'Coming soon',
    icon: Laptop,
    soon: true,
  },
  {
    key: 'cloud',
    title: 'A computer we run for you',
    body: 'Nothing to install. Ready in about a minute, with a home-style internet connection in your country.',
    badge: 'Coming soon',
    icon: Cloud,
    soon: true,
  },
  {
    key: 'own',
    title: 'A computer you already have',
    body: 'A Mac mini, an old laptop or a server that stays on. You, or someone who helps you, add it once.',
    badge: 'Works today',
    icon: Server,
  },
];

function OptionIcon({ icon: Icon }: { icon: (typeof OPTIONS)[number]['icon'] }) {
  return (
    <span aria-hidden="true" className="inline-flex size-8 items-center justify-center rounded-md bg-inset text-muted group-data-[state=checked]:bg-accent-soft group-data-[state=checked]:text-accent-text">
      <Icon className="size-4" />
    </span>
  );
}

/** The steps a helper follows, ready for the person's own mail app. */
function mailSteps(business: string, command: string | null, token: string | null) {
  const lines = [
    `Hi,`,
    ``,
    `I am setting up openajar for ${business}. It needs a computer that stays on (a Mac mini, an old laptop or a server). Could you add one for me?`,
    ``,
    `1. On that computer, open Terminal and run:`,
    `   ${command ?? '(open the setup page to get the command)'}`,
    `2. When it asks for a join token, paste:`,
    `   ${token ?? '(shown on the setup page)'}`,
    `   It works once, within an hour.`,
    `3. That is all. It turns green on my setup page by itself.`,
    ``,
    `Thank you!`,
  ];
  return `mailto:?subject=${encodeURIComponent(`Could you add a computer for ${business}?`)}&body=${encodeURIComponent(lines.join('\n'))}`;
}

export function ComputerStep({ ctx }: { ctx: StepContext }) {
  const { org, business } = ctx;
  const nodes = useNodes(org);
  const notServed = isNotServed(nodes.error);
  const flow = useAddNode({ org });
  // On a node there is no other computer to join, so no install command is made.
  const alone = useRunsAlone(org);
  const [choice, setChoice] = useState<Choice>('own');
  const [technical, setTechnical] = useState(false);
  const existing = nodes.data?.[0] ?? null;
  const connected = flow.connected ?? existing?.id ?? null;
  const name = business?.name ?? 'your business';

  // The command and its one-time token are made only when someone asks to see them.
  const wantsToken = technical && !alone && flow.ready && !flow.token && !flow.creating;
  useEffect(() => {
    if (wantsToken) flow.start();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantsToken]);

  // Emailing the steps needs the token in them, so it is made first and the mail opens after.
  const [mailing, setMailing] = useState(false);
  const mailto = mailSteps(name, flow.token?.install_command ?? null, flow.token?.token ?? null);
  useEffect(() => {
    if (!mailing || !flow.token) return;
    setMailing(false);
    window.location.href = mailto;
  }, [mailing, flow.token, mailto]);

  const soon = OPTIONS.find((item) => item.key === choice)?.soon;

  return (
    <SetupLayout
      org={org}
      business={business}
      step="computer"
      setup={ctx.setup}
      eyebrow={connected ? <Ready>{computerName(connected)} is ready</Ready> : undefined}
      title="Where should it work?"
      description="Your AI needs a computer of its own with a real browser, so websites see a normal visitor. Pick one. You can add more later."
      footer={
        <StepFooter
          ctx={ctx}
          step="computer"
          note={connected ? `${computerName(connected)} is ready.` : 'Continue unlocks when your computer says hello.'}
          later={connected ? false : undefined}
          canContinue={Boolean(connected) || notServed}
        />
      }
    >
      {notServed ? (
        <p className="rounded-lg border border-line bg-inset px-4 py-3 text-body text-muted">Adding a computer is not switched on here yet. You can do it later from Computers.</p>
      ) : (
        <div className="grid gap-6">
          <ChoiceGroup label="Where should it work?" value={choice} onValueChange={setChoice} columns={3}>
            {OPTIONS.map((option) => (
              <ChoiceCard
                key={option.key}
                value={option.key}
                layout="stacked"
                icon={<OptionIcon icon={option.icon} />}
                badge={<Badge tone={option.soon ? 'neutral' : 'ok'}>{option.badge}</Badge>}
                title={option.title}
                description={option.body}
              />
            ))}
          </ChoiceGroup>

          {soon ? (
            <div className="grid gap-4 rounded-xl border border-dashed border-line-strong p-4 sm:p-6">
              <p className="text-body">
                <span className="font-medium">Coming soon.</span>{' '}
                <span className="text-muted">
                  {choice === 'mac'
                    ? 'The Mac app will pair with one click, no Terminal. Until it is ready, use a computer you already have: it takes about three minutes.'
                    : 'A computer we run for you, with nothing to install. Until it is ready, use a computer you already have: it takes about three minutes.'}
                </span>
              </p>
              {choice === 'mac' ? (
                <ol aria-label="How the Mac app will work" className="grid gap-3 opacity-70 sm:grid-cols-3">
                  <PictureStep n={1} picture={<DownloadPicture />}>
                    Open the download
                  </PictureStep>
                  <PictureStep n={2} picture={<ComputerPicture />}>
                    Drag openajar to Applications
                  </PictureStep>
                  <PictureStep n={3} picture={<GreenPicture name="This Mac" />}>
                    Click Allow when the code matches
                  </PictureStep>
                </ol>
              ) : null}
              <Button className="justify-self-start" onClick={() => setChoice('own')}>
                Use a computer I already have
              </Button>
            </div>
          ) : (
            <div className="grid gap-4 rounded-xl border border-line p-4 sm:p-6">
              <div>
                <p className="text-lede font-medium">How it works</p>
                <p className="mt-1 text-body text-muted">A small program goes on the computer. It keeps a private browser for each account and only talks to openajar.</p>
              </div>
              <ol aria-label="How it works" className="grid gap-3 sm:grid-cols-3">
                <PictureStep n={1} picture={<ComputerPicture />}>
                  Pick a computer that stays on
                </PictureStep>
                <PictureStep n={2} picture={<TerminalPicture />}>
                  Paste one line into its Terminal
                </PictureStep>
                <PictureStep n={3} picture={<GreenPicture />}>
                  It turns green here, by itself
                </PictureStep>
              </ol>

              <div role="status" className={cn('flex min-h-12 flex-wrap items-center gap-3 rounded-lg border px-4 py-3 text-body', connected ? 'border-(--ok-soft) bg-(--ok-soft)' : 'border-line')}>
                {connected ? (
                  <DoneRow>
                    <span>
                      <span className="font-medium">{computerName(connected)}</span> is ready.
                    </span>
                  </DoneRow>
                ) : (
                  <>
                    <span className="mr-auto flex items-center gap-2 text-muted">
                      <span aria-hidden="true" className="size-2 shrink-0 animate-pulse-dot rounded-full bg-fg" />
                      Waiting for your computer. It turns green here by itself.
                    </span>
                    <a
                      href={mailto}
                      onClick={(event) => {
                        if (flow.token) return;
                        event.preventDefault();
                        setTechnical(true);
                        setMailing(true);
                      }}
                      className="text-secondary text-muted underline decoration-line-strong underline-offset-4 hover:text-fg"
                    >
                      Email the steps to someone who helps me
                    </a>
                  </>
                )}
              </div>

                <div className="border-t border-line pt-4">
                  <button
                    type="button"
                    aria-expanded={technical}
                    onClick={() => setTechnical(!technical)}
                    className="inline-flex items-center gap-2 text-label text-fg"
                  >
                    <ChevronDown className={cn('size-4 text-faint transition-transform duration-(--duration-fast)', technical && 'rotate-180')} aria-hidden="true" />
                    I'm technical: show me the command
                  </button>
                  {technical ? (
                    <div className="mt-4">
                      {flow.token ? (
                        <div className="grid gap-4">
                          <div className="grid gap-2">
                            <p className="text-secondary font-medium">Run this in the computer's Terminal</p>
                            <CodeBlock code={flow.token.install_command ?? ''} label="Copy install command" />
                          </div>
                          {/* On its own, so it never lands in shell history or the process list (security review 10). */}
                          <div className="grid gap-2">
                            <p className="text-secondary font-medium">Paste this when it asks for a join token</p>
                            {flow.token.token ? <CodeBlock code={flow.token.token} label="Copy join token" /> : null}
                            <p className="text-secondary text-faint">It works once, for the next {timeUntil(flow.token.expires_at)}. Coming back here later makes a fresh one.</p>
                          </div>
                        </div>
                      ) : (
                        <div className="skeleton h-40 rounded-lg" />
                      )}
                    </div>
                  ) : null}
                </div>
            </div>
          )}
        </div>
      )}
    </SetupLayout>
  );
}
