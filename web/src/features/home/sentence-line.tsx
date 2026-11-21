/** Home's line under its sentence: each part a link where it leads somewhere (a check, a machine, an account, Needs you). */
import { Link } from '@tanstack/react-router';
import { cn } from '@/lib/cn';
import type { LinePart } from './sentence';

export const inline = 'underline decoration-line-strong underline-offset-4 hover:decoration-fg';

/** One part of the line under the sentence: a link where it leads somewhere. */
function Part({ org, part }: { org: string; part: LinePart }) {
  switch (part.kind) {
    case 'trouble':
      if (part.to === 'check')
        return (
          <Link to="/$org/checks/$id" params={{ org, id: part.id }} className={cn(inline, 'text-fg')}>
            {part.text}
          </Link>
        );
      if (part.to === 'machine')
        return (
          <Link to="/$org/nodes/$id" params={{ org, id: part.id }} className={cn(inline, 'text-fg')}>
            {part.text}
          </Link>
        );
      {
        const [ws = '', name = ''] = part.id.split('/');
        return (
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className={cn(inline, 'text-fg')}>
            {part.text}
          </Link>
        );
      }
    case 'needs':
      return (
        <>
          <Link to="/$org/needs-you" params={{ org }} className={cn(inline, 'whitespace-nowrap text-fg')}>
            {part.text}
          </Link>
          {part.rest}
        </>
      );
    default:
      return <>{part.text}</>;
  }
}

/** The line under Home's sentence: each part a link where it leads somewhere. */
export function SentenceLine({ org, line }: { org: string; line: LinePart[] }) {
  return (
    <>
      {line.map((part, index) => (
        <span key={index}>
          <Part org={org} part={part} />.{index < line.length - 1 ? ' ' : ''}
        </span>
      ))}
    </>
  );
}
