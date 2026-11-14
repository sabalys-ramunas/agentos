/**
 * The parts of a product profile's page: Components first (the design system's components, their use
 * on checked pages and the look-alikes, from the newest system check), then the tokens rendered from
 * the profile's own values (colour light and dark, type, spacing, corners), guidelines, references and
 * environments. Every colour, size and family shown here is the profile's data, marked for studio as such.
 */
import { Link } from '@tanstack/react-router';
import { Boxes, ExternalLink, ScanSearch } from 'lucide-react';
import type { CSSProperties, ReactNode } from 'react';
import { isStudioMissing, useBrandComponents } from '@/api/hooks';
import type { BrandProfile, BrandToken } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { Card, CardHeader } from '@/components/ui/card';
import { CopyButton } from '@/components/ui/misc';
import { Skeleton, EmptyState, ErrorState } from '@/components/ui/states';
import { StatStrip, StatTile } from '@/components/ui/stat-tile';
import { Status } from '@/components/ui/status';
import { Swatch, TokenChip } from '@/components/ui/swatch';
import { cn } from '@/lib/cn';
import { plural, safeUrl, timeAgo } from '@/lib/format';
import { lookAlikes, percent } from '@/features/checks/model';
import { StudioMissingState } from '@/features/checks/parts';
import type { DrawnButton } from './model';
import { drawnButton, grouped, guidelineRule, pairColours, paintable, pixels, resolver } from './model';

/** A section of the page with its anchor, so the nav and `?section=` reach it. */
export function Part({ id, title, meta, children }: { id: string; title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <section id={`section-${id}`} aria-labelledby={`h-${id}`} className="scroll-mt-20">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id={`h-${id}`} className="text-section-title">
          {title}
        </h2>
        {meta ? <span className="text-secondary text-faint">{meta}</span> : null}
      </div>
      {children}
    </section>
  );
}

/** A Button drawn from a profile's tokens, as data: marked for studio, never a screenshot. */
export function DrawnButtonView({ button, label, text }: { button: DrawnButton; label: string; text: string }) {
  const style: CSSProperties = {
    background: button.background,
    color: button.color,
    borderRadius: button.radius ?? undefined,
    padding: button.paddingY && button.paddingX ? `${button.paddingY} ${button.paddingX}` : undefined,
    fontFamily: button.font ?? undefined,
    fontSize: button.size ?? undefined,
  };
  return (
    <div
      data-studio="ignore"
      data-studio-why="a button drawn from a product's tokens: another brand's values"
      className="grid min-h-24 place-items-center rounded-lg border border-dashed border-line bg-inset p-4"
      role="img"
      aria-label={label}
    >
      <span inert style={style} className="inline-flex items-center font-medium">
        {text}
      </span>
    </div>
  );
}

function DrawnSample({ profile }: { profile: BrandProfile }) {
  const button = drawnButton(profile);
  if (!button) return null;
  return (
    <div className="grid gap-2 border-b border-line px-5 py-4">
      <DrawnButtonView button={button} label={`A button drawn from ${profile.display_name}'s tokens`} text="Button" />
      <p className="text-caption text-faint">
        Drawn from tokens: {button.from.join(', ')}. Not a screenshot; the font is the one this computer has.
      </p>
    </div>
  );
}

export function Components({ org, profile, picked }: { org: string; profile: BrandProfile; picked?: string }) {
  const query = useBrandComponents(org, profile.ref);
  const defined = profile.brand?.components ?? [];
  const data = query.data;
  const loaded = data && !isStudioMissing(data) ? data : null;
  const used = new Map((loaded?.components ?? []).map((item) => [item.name, item]));
  const names = [...new Set([...defined.map((item) => item.name), ...used.keys()])].filter(Boolean);
  const maxUses = Math.max(1, ...[...used.values()].map((item) => item.uses));
  const totalUses = [...used.values()].reduce((sum, item) => sum + item.uses, 0);
  const shares = Object.values(loaded?.on_tokens ?? {});
  const share = shares.length ? shares.reduce((a, b) => a + b, 0) / shares.length : null;
  const near = lookAlikes(loaded?.look_alike_findings ?? []);

  let body: ReactNode;
  if (query.isPending) {
    body = (
      <div className="grid gap-4" role="status" aria-label="Loading components">
        <Skeleton className="h-24 rounded-xl" />
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-48 rounded-xl" />
          <Skeleton className="h-48 rounded-xl" />
        </div>
      </div>
    );
  } else if (query.isError) {
    body = (
      <Card>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    );
  } else if (data && isStudioMissing(data)) {
    body = (
      <Card>
        <StudioMissingState missing={data} />
      </Card>
    );
  } else if (names.length === 0) {
    body = (
      <Card>
        <EmptyState
          icon={<Boxes />}
          title="No components named yet"
          description="Name the design system's components in the profile (how each is found on a page), and a system check counts their use and the hand-rolled look-alikes."
        />
      </Card>
    );
  } else {
    body = (
      <div className="grid gap-4">
        {loaded?.run ? (
          <StatStrip>
            <StatTile label="Component uses" value={totalUses.toLocaleString('en')} hint={`on the screens of the newest system check`} />
            <StatTile label="Hand-rolled look-alikes" value={loaded.hand_rolled} tone={loaded.hand_rolled ? 'warn' : undefined} hint="controls drawn outside every component" />
            <StatTile label="On the system" value={share === null ? '–' : percent(share)} hint="values on a token, all categories" />
            <StatTile label="Values that only look like a token" value={loaded.look_alikes?.look_alike ?? near.length} hint={loaded.look_alikes ? `of ${loaded.look_alikes.token + loaded.look_alikes.derived + loaded.look_alikes.look_alike} measured` : 'found by the system check'} />
          </StatStrip>
        ) : (
          <Card className="flex flex-wrap items-center gap-3 px-5 py-4">
            <ScanSearch aria-hidden="true" className="size-4 text-muted" />
            <p className="min-w-0 flex-1 text-14 text-muted">
              No system check of {profile.ref} is kept yet, so use and look-alikes are not known. Ask your agent for one.
            </p>
            <CopyButton text={`ui_check url=${profile.website ?? 'http://localhost:3000/'} profile=${profile.ref}`} label="Copy a system check for your agent" size="md" />
          </Card>
        )}
        <div className="grid gap-4 md:grid-cols-2">
          {names.map((name) => {
            const def = defined.find((item) => item.name === name);
            const use = used.get(name);
            const isButton = /button/i.test(name) && name === names.find((item) => /button/i.test(item));
            return (
              <Card key={name} id={`component-${name}`} className={cn('min-w-0 overflow-hidden', picked === name && 'ring-2 ring-fg', isButton && 'md:col-span-2')}>
                <CardHeader
                  as="h3"
                  title={name}
                  meta={def?.attr ?? def?.class_pattern ?? undefined}
                  action={
                    use ? (
                      use.findings ? (
                        <Status status={{ label: plural(use.findings, 'finding'), tone: 'warn' }} />
                      ) : use.uses ? (
                        <Status status={{ label: 'No findings', tone: 'ok' }} />
                      ) : (
                        <Status status={{ label: 'Not seen', tone: 'idle' }} />
                      )
                    ) : undefined
                  }
                />
                {isButton ? <DrawnSample profile={profile} /> : null}
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-3 p-5">
                  <dt className="text-secondary text-faint">Found by</dt>
                  <dd className="min-w-0">
                    {def?.attr || def?.class_pattern ? (
                      <TokenChip name={def.attr ? `[${def.attr}]` : `class ${def.class_pattern}`} />
                    ) : (
                      <span className="text-14 text-muted">Its marker in the page</span>
                    )}
                  </dd>
                  <dt className="text-secondary text-faint">Used</dt>
                  <dd className="flex min-w-0 items-center gap-3">
                    {use ? (
                      <>
                        <span className="h-1 w-full max-w-48 overflow-hidden rounded-full bg-chart-grid">
                          <span className="block h-full rounded-full bg-chart-bar-strong" style={{ width: `${Math.round((use.uses / maxUses) * 100)}%` }} />
                        </span>
                        <span className="shrink-0 text-14 text-fg">
                          <span className="font-mono tabular">{use.uses}</span>
                          <span className="text-muted"> on {plural(use.screens, 'screen')}</span>
                        </span>
                      </>
                    ) : (
                      <span className="text-14 text-muted">{loaded?.run ? 'Not seen on the checked screens' : 'Not measured yet'}</span>
                    )}
                  </dd>
                </dl>
              </Card>
            );
          })}
        </div>
        {near.length ? (
          <Card>
            <CardHeader title="Values that only look like a token" meta="hard-coded copies of a token's value: they will not follow a change to it" />
            <ul>
              {near.map((item) => {
                const colour = item.category === 'color' ? paintable(item.value) : null;
                return (
                  <li key={item.finding.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-5 py-3 last:border-b-0">
                    {colour ? (
                      <span data-studio="ignore" role="img" aria-label={`The colour ${item.value}`} className="size-4 shrink-0 rounded-sm border border-line" style={{ background: colour }} />
                    ) : null}
                    <span className="font-mono text-code text-fg">{item.value}</span>
                    <span className="text-14 text-muted">
                      should be <span className="font-mono text-code">{item.token ?? 'a token'}</span>
                    </span>
                    <span className="min-w-0 truncate font-mono text-code-caption text-faint" title={item.finding.where.selector}>
                      {item.finding.component ? `${item.finding.component}, ` : ''}
                      {item.finding.where.selector}
                    </span>
                    <span className="ml-auto text-caption text-faint tabular">{[item.distance, plural(item.count, 'place')].filter(Boolean).join(', ')}</span>
                  </li>
                );
              })}
            </ul>
          </Card>
        ) : null}
        {loaded?.run ? (
          <p className="text-caption text-faint">
            From the system check of{' '}
            <Link to="/$org/checks/$id" params={{ org, id: loaded.run }} className="underline decoration-line-strong underline-offset-2 hover:text-fg">
              {loaded.url ?? 'the page'}
            </Link>
            {loaded.checked_at ? `, ${timeAgo(loaded.checked_at).toLowerCase()}` : ''}.
          </p>
        ) : null}
      </div>
    );
  }
  return (
    <Part id="components" title="Components" meta={names.length ? `${plural(names.length, 'component')} in ${profile.ref}` : undefined}>
      {body}
    </Part>
  );
}

function TokenName({ token }: { token: BrandToken }) {
  return (
    <span className="block truncate font-mono text-code-caption text-muted" title={token.name}>
      {token.name}
    </span>
  );
}

export function Colours({ profile }: { profile: BrandProfile }) {
  const tokens = profile.brand?.tokens ?? [];
  const resolve = resolver(tokens);
  const { colour } = grouped(tokens);
  const { pairs, single } = pairColours(colour);
  return (
    <Part id="colour" title="Colour" meta={colour.length ? plural(colour.length, 'colour token') : undefined}>
      {colour.length === 0 ? (
        <Card>
          <EmptyState title="No colour tokens" description="The profile names no colours. An agent can read them from the running site; you approve the draft." />
        </Card>
      ) : (
        <div className="grid gap-4">
          {single.length ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {single.map((token) => {
                const value = resolve(token.value);
                const paint = paintable(value);
                const alias = value !== token.value ? `${token.value}, ${value}` : value;
                return paint ? (
                  <Swatch key={token.name} color={paint} name={token.name} value={alias} />
                ) : (
                  <Card key={token.name} flat className="grid gap-1 p-3">
                    <TokenName token={token} />
                    <span className="font-mono text-code-caption break-words text-faint">{token.value}</span>
                  </Card>
                );
              })}
            </div>
          ) : null}
          {pairs.length ? (
            <Card>
              <CardHeader title="Light and dark" meta="each colour by its job, in both themes" />
              <ul className="grid gap-px sm:grid-cols-2 lg:grid-cols-3">
                {pairs.map((pair) => (
                  <li key={pair.name} className="flex min-w-0 items-center gap-3 px-5 py-3">
                    <span className="flex shrink-0 overflow-hidden rounded-md border border-line" aria-hidden="true">
                      {[pair.light, pair.dark].map((token, index) => {
                        const paint = token ? paintable(resolve(token.value)) : null;
                        return <span key={index} data-studio="ignore" className={cn('block size-8', !paint && 'bg-inset')} style={paint ? { background: paint } : undefined} />;
                      })}
                    </span>
                    <span className="grid min-w-0">
                      <span className="truncate font-mono text-code-caption text-fg" title={pair.name}>
                        {pair.name}
                      </span>
                      <span className="truncate font-mono text-micro text-faint">
                        {pair.light ? resolve(pair.light.value) : 'no light'} · {pair.dark ? resolve(pair.dark.value) : 'no dark'}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>
      )}
    </Part>
  );
}

export function TypeAndSpace({ profile }: { profile: BrandProfile }) {
  const tokens = profile.brand?.tokens ?? [];
  const resolve = resolver(tokens);
  const { type, space, radius } = grouped(tokens);
  const fonts = profile.brand?.fonts ?? [];
  const families = type.filter((token) => /family|^font\.[a-z-]+$/i.test(token.name) && !/size|weight|height/i.test(token.name));
  const sizes = type.filter((token) => /size/i.test(token.name) && pixels(resolve(token.value)) !== null);
  const familyFor = (role: string | null) => {
    const font = fonts.find((item) => item.role === role) ?? fonts[0];
    return font?.family && /^[\w\s-]+$/.test(font.family) ? font.family : undefined;
  };
  return (
    <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
      <Part id="type" title="Type" meta={fonts.length ? fonts.map((font) => `${font.family}${font.weights.length ? ` ${font.weights.join(' ')}` : ''}`).join(', ') : undefined}>
        <Card>
          {fonts.length === 0 && type.length === 0 ? (
            <EmptyState title="No type yet" description="The profile names no fonts or type tokens." />
          ) : (
            <ul>
              {fonts.map((font) => (
                <li key={`${font.family}-${font.role}`} className="grid gap-1 border-b border-line px-5 py-4 last:border-b-0">
                  <span className="flex items-center gap-2 text-caption text-faint">
                    {font.role ?? 'font'} <span className="font-mono text-code-caption">{font.weights.join(' ')}</span>
                  </span>
                  <span data-studio="ignore" className="truncate text-24 text-fg" style={{ fontFamily: familyFor(font.role) ? `"${font.family}"` : undefined }}>
                    {font.family}
                  </span>
                </li>
              ))}
              {[...families, ...sizes].map((token) => {
                const value = resolve(token.value);
                const size = pixels(value);
                return (
                  <li key={token.name} className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)_auto] items-baseline gap-3 border-b border-line px-5 py-3 last:border-b-0">
                    <TokenName token={token} />
                    <span
                      data-studio="ignore"
                      className="truncate text-fg"
                      style={size !== null ? { fontSize: Math.min(size, 40), fontFamily: familyFor('body') } : { fontFamily: /^[\w\s,"'-]+$/.test(value) ? value : undefined }}
                    >
                      {profile.headline ?? profile.display_name}
                    </span>
                    <span className="font-mono text-code-caption text-faint">{value}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </Part>
      <Part id="space" title="Spacing and corners">
        <Card>
          {space.length === 0 && radius.length === 0 ? (
            <EmptyState title="No spacing or corners yet" description="The profile names no spacing or radius tokens." />
          ) : (
            <div className="grid gap-4 p-5">
              {space.length ? (
                <ul className="grid gap-2">
                  {space.map((token) => {
                    const value = resolve(token.value);
                    const px = pixels(value);
                    return (
                      <li key={token.name} className="grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)_auto] items-center gap-3">
                        <TokenName token={token} />
                        <span className="h-3 min-w-0">
                          {px !== null ? <span data-studio="ignore" className="block h-3 rounded-sm bg-chart-bar" style={{ width: Math.min(px * 3, 240) }} /> : null}
                        </span>
                        <span className="font-mono text-code-caption text-faint tabular">{value}</span>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
              {radius.length ? (
                <ul className="flex flex-wrap gap-4">
                  {radius.map((token) => {
                    const value = resolve(token.value);
                    const px = pixels(value);
                    return (
                      <li key={token.name} className="grid justify-items-center gap-1">
                        <span data-studio="ignore" className="block size-12 border-2 border-line-strong bg-inset" style={{ borderRadius: px !== null ? Math.min(px, 24) : undefined }} />
                        <span className="max-w-24 truncate font-mono text-micro text-faint" title={`${token.name}: ${value}`}>
                          {token.name.replace(/^radius\.|^radii\./, '')} · {value}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </div>
          )}
        </Card>
      </Part>
    </div>
  );
}

export function Guidelines({ profile }: { profile: BrandProfile }) {
  return (
    <Part id="guidelines" title="Guidelines" meta="checked ones are measured on every check of this product">
      <Card>
        {profile.guidelines.length === 0 ? (
          <EmptyState title="No guidelines yet" description="Guidelines tell agents and reviewers what this product holds to. A checked one is measured on every page." />
        ) : (
          <ul>
            {profile.guidelines.map((item) => (
              <li key={item.id} className="flex items-start gap-3 border-b border-line px-5 py-3 last:border-b-0">
                <span className="grid min-w-0 flex-1 gap-1">
                  <span className="text-14 break-words text-fg">{item.text}</span>
                  <span className="text-secondary text-faint">{guidelineRule(item)}</span>
                </span>
                {item.advisory || !item.check ? <Badge>Advice</Badge> : <Badge className="font-mono">{item.check}</Badge>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </Part>
  );
}

export function References({ profile }: { profile: BrandProfile }) {
  return (
    <div className="grid items-start gap-8 lg:grid-cols-2">
      <Part id="references" title="References" meta="products to compare with">
        <Card>
          {profile.references.length === 0 ? (
            <EmptyState title="No references yet" description="References are products this one is compared with: a density, a tone, a flow." />
          ) : (
            <ul>
              {profile.references.map((item) => {
                const href = safeUrl(item.url);
                return (
                  <li key={item.name} className="grid gap-1 border-b border-line px-5 py-3 last:border-b-0">
                    <span className="text-14 font-medium text-fg">{item.name}</span>
                    {href ? (
                      <a href={href} target="_blank" rel="noreferrer noopener" className="inline-flex min-w-0 items-center gap-1 font-mono text-code-caption text-muted hover:text-fg">
                        <span className="truncate" title={item.url}>
                          {item.url}
                        </span>
                        <ExternalLink aria-hidden="true" className="size-3 shrink-0" />
                      </a>
                    ) : (
                      <span className="truncate font-mono text-code-caption text-faint">{item.url}</span>
                    )}
                    {item.note ? <span className="text-secondary text-muted">{item.note}</span> : null}
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </Part>
      <Part id="environments" title="Environments" meta="where it runs, by name">
        <Card>
          {profile.environments.length === 0 ? (
            <EmptyState title="No environments yet" description="Name where the product runs (local, staging) so checks find the right profile from a url." />
          ) : (
            <ul>
              {profile.environments.map((item) => (
                <li key={item.name} className="flex min-w-0 items-center gap-3 border-b border-line px-5 py-3 last:border-b-0">
                  <span className="w-24 shrink-0 truncate text-14 font-medium text-fg">{item.name}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-code-caption text-muted" title={item.url}>
                    {item.url}
                  </span>
                  <CopyButton text={item.url} label={`Copy the ${item.name} address`} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      </Part>
    </div>
  );
}
