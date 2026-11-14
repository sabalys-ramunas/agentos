import { clsx } from 'clsx';
import type { ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

// tailwind-merge has to be told our type scale and type roles are font-size, or it reads text-13
// or text-body as a colour and drops it next to text-fg.
const merge = extendTailwindMerge({
  extend: {
    theme: {
      text: [
        '11', '12', '13', '14', '16', '20', '24', '32', '40', '56',
        'display', 'page-title', 'page-title-compact', 'section-title', 'lede', 'card-title', 'table-header',
        'label', 'body', 'secondary', 'caption', 'number', 'stat', 'code', 'code-caption', 'eyebrow', 'micro',
      ],
    },
  },
});

export function cn(...values: ClassValue[]) {
  return merge(clsx(values));
}
