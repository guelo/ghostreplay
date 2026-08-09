import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { UploadCommitSource } from "../services/GameAnalysisCoordinator";
import { useSessionUploadCommitRevision } from "./useSessionUploadCommitRevision";

class TestUploadCommitSource implements UploadCommitSource {
  sessionId: string | null = null;
  revision = 0;
  listeners = new Set<() => void>();
  unsubscribeSpy = vi.fn();

  getUploadCommitRevision(sessionId: string | null): number {
    return sessionId !== null && sessionId === this.sessionId
      ? this.revision
      : 0;
  }

  addUploadCommitListener(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
      this.unsubscribeSpy();
    };
  }

  install(sessionId: string, revision = 0) {
    this.sessionId = sessionId;
    this.revision = revision;
  }

  publish() {
    this.revision += 1;
    for (const listener of this.listeners) listener();
  }
}

describe("useSessionUploadCommitRevision", () => {
  it("exposes the current revision and rerenders when the source publishes", () => {
    const source = new TestUploadCommitSource();
    source.install("session-a", 2);
    const { result } = renderHook(() =>
      useSessionUploadCommitRevision(source, "session-a"),
    );

    expect(result.current).toBe(2);
    act(() => source.publish());
    expect(result.current).toBe(3);
  });

  it("changes or clears the session without exposing the old revision", () => {
    const source = new TestUploadCommitSource();
    source.install("session-a", 4);
    const { result, rerender } = renderHook(
      ({ sessionId }: { sessionId: string | null }) =>
        useSessionUploadCommitRevision(source, sessionId),
      { initialProps: { sessionId: "session-a" as string | null } },
    );
    expect(result.current).toBe(4);

    source.install("session-b", 7);
    rerender({ sessionId: "session-b" });
    expect(result.current).toBe(7);

    rerender({ sessionId: null });
    expect(result.current).toBe(0);

    act(() => {
      source.install("session-a", 9);
      source.publish();
    });
    expect(result.current).toBe(0);
  });

  it("unsubscribes cleanly on unmount", () => {
    const source = new TestUploadCommitSource();
    source.install("session-a");
    const { unmount } = renderHook(() =>
      useSessionUploadCommitRevision(source, "session-a"),
    );

    expect(source.listeners.size).toBe(1);
    unmount();
    expect(source.listeners.size).toBe(0);
    expect(source.unsubscribeSpy).toHaveBeenCalledTimes(1);
  });
});
