import { useEffect, useRef, useState } from 'react';
import { IDLE, StreamController } from './stream';
import type { ControllerOptions, StreamState } from './stream';

/**
 * One relay connection for as long as the screen is mounted. A new `url` (view to control)
 * opens a new connection behind the live one; null closes it.
 */
export function useStream(
  url: string | null,
  attach: ControllerOptions['attach'],
  probe?: ControllerOptions['probe'],
  onControlLost?: () => void,
) {
  const [state, setState] = useState<StreamState>(IDLE);
  const handlers = useRef({ attach, probe, onControlLost });
  useEffect(() => {
    handlers.current = { attach, probe, onControlLost };
  });
  const controller = useRef<StreamController | null>(null);
  useEffect(() => {
    const created = new StreamController({
      attach: (socket, live) => handlers.current.attach(socket, live),
      probe: async () => (await handlers.current.probe?.()) ?? null,
      onControlLost: () => handlers.current.onControlLost?.(),
      onChange: setState,
    });
    controller.current = created;
    return () => {
      created.dispose();
      controller.current = null;
    };
  }, []);
  useEffect(() => {
    controller.current?.setUrl(url);
  }, [url]);
  return { state, retry: () => controller.current?.retry() };
}
