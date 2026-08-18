import {
  getOpeningRoots,
  type OpeningRootsListResponse,
} from "../utils/api";
import {
  buildRootIndex,
  type LiveOpeningRootIndex,
} from "./deriveLiveLineage";

type OpeningRootFamilies = OpeningRootsListResponse["families"];

interface OpeningRootsCacheEntry {
  families: OpeningRootFamilies;
  index: LiveOpeningRootIndex;
}

/**
 * App-lifetime owner for the decoded opening-root registry and its key index.
 *
 * The in-flight promise is cached so consumers mounting concurrently share the
 * same request and JSON decode. A fulfilled promise remains cached for the
 * JavaScript module lifetime; a rejected one is evicted so a later consumer
 * can retry.
 */
let cachePromise: Promise<OpeningRootsCacheEntry> | null = null;

function loadOpeningRootsCacheEntry(): Promise<OpeningRootsCacheEntry> {
  if (cachePromise) return cachePromise;

  const requestPromise = getOpeningRoots().then((response) => ({
    families: response.families,
    index: buildRootIndex(response),
  }));
  const nextPromise: Promise<OpeningRootsCacheEntry> = requestPromise.catch(
    (error: unknown) => {
      // A pre-reset request may settle after a new request has become active.
      // It must not evict that newer entry.
      if (cachePromise === nextPromise) cachePromise = null;
      throw error;
    },
  );

  cachePromise = nextPromise;
  return nextPromise;
}

export async function loadOpeningRootIndex(): Promise<LiveOpeningRootIndex> {
  return (await loadOpeningRootsCacheEntry()).index;
}

export async function loadOpeningRootFamilies(): Promise<OpeningRootFamilies> {
  return (await loadOpeningRootsCacheEntry()).families;
}

/** Deterministic invalidation seam for tests; production never resets it. */
export function resetOpeningRootsCacheForTests(): void {
  cachePromise = null;
}
