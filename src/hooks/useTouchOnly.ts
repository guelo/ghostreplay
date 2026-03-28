import { useSyncExternalStore } from 'react';

const QUERY = '(hover: none) and (pointer: coarse)';

function subscribe(cb: () => void) {
  const mql = window.matchMedia(QUERY);
  mql.addEventListener('change', cb);
  return () => mql.removeEventListener('change', cb);
}

function getSnapshot() {
  return window.matchMedia(QUERY).matches;
}

/** Reactively tracks whether the device is touch-only (no hover). */
export function useTouchOnly(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot);
}
