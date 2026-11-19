import { clsx } from 'clsx';
import type { ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

// tailwind-merge has to be told our type roles are font-size, or it reads text-body as a colour and
// drops it next to text-fg. (40 and 56 are left from the old scale: a few headings still name them.)
const merge = extendTailwindMerge({
  extend: {
    theme: {
      text: [
        '40', '56',
        'display', 'page-title', 'page-title-compact', 'headline', 'heading', 'section-title', 'lede', 'card-title', 'table-header',
        'label', 'body', 'secondary', 'caption', 'number', 'stat', 'code', 'code-caption', 'eyebrow', 'micro',
      ],
    },
  },
});

export function cn(...values: ClassValue[]) {
  return merge(clsx(values));
}
