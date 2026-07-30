// Tiny cross-page navigation bus. Pages request a tab switch (with an optional
// payload) without prop-drilling setTab through App — e.g. a strategy row's
// "Optimize" button jumps to the Optimizer tab with that strategy preselected.
export interface NavRequest {
  tab: string;
  strategyId?: number;
}

let pending: NavRequest | null = null;
const listeners = new Set<(r: NavRequest) => void>();

export function requestNav(r: NavRequest): void {
  pending = r;
  listeners.forEach((l) => l(r));
}

export function onNav(l: (r: NavRequest) => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/** One-shot read of the payload by the page that mounts in response. */
export function consumeNav(): NavRequest | null {
  const p = pending;
  pending = null;
  return p;
}
