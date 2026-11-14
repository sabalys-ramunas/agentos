import type { Download } from '@/api/http';

/**
 * Saves a file the node sent, under the name the node gave it or the one we keep. The bytes are only ever
 * handed to the browser's download, so nothing in the app opens them.
 */
export function saveFile({ blob, filename }: Download, fallback: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename ?? fallback;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
