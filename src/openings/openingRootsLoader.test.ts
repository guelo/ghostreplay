import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  OpeningRootItem,
  OpeningRootsListResponse,
} from "../utils/api";
import {
  loadOpeningRootFamilies,
  loadOpeningRootIndex,
  resetOpeningRootsCacheForTests,
} from "./openingRootsLoader";

const getOpeningRootsMock = vi.fn();

vi.mock("../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/api")>();
  return {
    ...actual,
    getOpeningRoots: (...args: unknown[]) => getOpeningRootsMock(...args),
  };
});

const root: OpeningRootItem = {
  opening_key: "root-key",
  opening_name: "Root Opening",
  opening_family: "Root Family",
  eco: "A00",
  depth: 1,
};

const response: OpeningRootsListResponse = {
  families: [{ family_name: "Root Family", roots: [root] }],
  total_roots: 1,
  total_families: 1,
};

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("openingRootsLoader", () => {
  beforeEach(() => {
    resetOpeningRootsCacheForTests();
    getOpeningRootsMock.mockReset();
  });

  it("shares one decoded registry and stable projections across consumers", async () => {
    const pending = deferred<OpeningRootsListResponse>();
    getOpeningRootsMock.mockReturnValue(pending.promise);

    const indexLoad = loadOpeningRootIndex();
    const familiesLoad = loadOpeningRootFamilies();

    expect(getOpeningRootsMock).toHaveBeenCalledTimes(1);
    pending.resolve(response);

    const [index, families] = await Promise.all([indexLoad, familiesLoad]);
    expect(families).toBe(response.families);
    expect(index.get(root.opening_key)).toBe(root);
    expect(await loadOpeningRootIndex()).toBe(index);
    expect(await loadOpeningRootFamilies()).toBe(families);
    expect(getOpeningRootsMock).toHaveBeenCalledTimes(1);
  });

  it("evicts a shared rejection so either projection can retry", async () => {
    const failure = new Error("boom");
    getOpeningRootsMock
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce(response);

    const results = await Promise.allSettled([
      loadOpeningRootIndex(),
      loadOpeningRootFamilies(),
    ]);

    expect(results).toEqual([
      { status: "rejected", reason: failure },
      { status: "rejected", reason: failure },
    ]);
    expect(await loadOpeningRootFamilies()).toBe(response.families);
    expect(getOpeningRootsMock).toHaveBeenCalledTimes(2);
  });

  it("reset creates a new cache entry without mutating prior results", async () => {
    getOpeningRootsMock.mockResolvedValueOnce(response);
    const firstIndex = await loadOpeningRootIndex();

    const nextRoot = { ...root, opening_key: "next-root-key" };
    const nextResponse: OpeningRootsListResponse = {
      families: [{ family_name: "Next Family", roots: [nextRoot] }],
      total_roots: 1,
      total_families: 1,
    };
    resetOpeningRootsCacheForTests();
    getOpeningRootsMock.mockResolvedValueOnce(nextResponse);

    const nextIndex = await loadOpeningRootIndex();
    expect(nextIndex).not.toBe(firstIndex);
    expect(nextIndex.get(nextRoot.opening_key)).toBe(nextRoot);
    expect(firstIndex.get(root.opening_key)).toBe(root);
    expect(getOpeningRootsMock).toHaveBeenCalledTimes(2);
  });

  it("a pre-reset late rejection cannot evict the newer cache entry", async () => {
    const stale = deferred<OpeningRootsListResponse>();
    getOpeningRootsMock
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(response);

    const staleLoad = loadOpeningRootFamilies();
    resetOpeningRootsCacheForTests();
    const currentIndex = await loadOpeningRootIndex();

    const failure = new Error("stale failure");
    stale.reject(failure);
    await expect(staleLoad).rejects.toBe(failure);

    expect(await loadOpeningRootIndex()).toBe(currentIndex);
    expect(getOpeningRootsMock).toHaveBeenCalledTimes(2);
  });
});
