import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  OpeningBoundarySnapshot,
  OpeningBoundarySource,
} from "../services/GameAnalysisCoordinator";
import { useGameStore } from "../stores/useGameStore";

const pollMock = vi.fn(
  (_sessionId: string, _trigger: string, _options?: { boundaryToken?: string }) =>
    Promise.resolve(),
);
const abortMock = vi.fn((_sessionId: string, _token: string) => undefined);
const captureMock = vi.fn();
vi.mock("../utils/openingDeltaPoll", () => ({
  pollFreshOpeningDelta: (
    sessionId: string,
    trigger: string,
    options?: { boundaryToken?: string },
  ) => pollMock(sessionId, trigger, options),
  abortOpeningBoundaryDeltaPoll: (sessionId: string, token: string) =>
    abortMock(sessionId, token),
}));
vi.mock("../analytics/posthog", () => ({
  captureEvent: (event: string, properties?: Record<string, unknown>) =>
    captureMock(event, properties),
}));

import { useOpeningBoundaryDelta } from "./useOpeningBoundaryDelta";

describe("useOpeningBoundaryDelta", () => {
  let revision = 0;
  let snapshot: OpeningBoundarySnapshot | null = null;
  let listeners: Set<() => void>;
  let source: OpeningBoundarySource;
  const initialStoreState = useGameStore.getInitialState();

  beforeEach(() => {
    useGameStore.setState({ ...initialStoreState }, true);
    revision = 0;
    snapshot = null;
    listeners = new Set();
    pollMock.mockClear();
    abortMock.mockClear();
    captureMock.mockClear();
    source = {
      getOpeningBoundaryRevision: () => revision,
      getOpeningBoundarySnapshot: (sessionId) =>
        snapshot?.sessionId === sessionId ? snapshot : null,
      addOpeningBoundaryListener: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
  });

  it("starts the matching mode poll and cancels only its token on cleanup", () => {
    const { unmount } = renderHook(() =>
      useOpeningBoundaryDelta(source, "session-1", true),
    );
    snapshot = {
      sessionId: "session-1",
      openingMiddlePly: 17,
      reconciliationToken: "a".repeat(64),
      transitionRevision: 1,
    };
    revision = 1;
    act(() => {
      for (const listener of listeners) listener();
    });

    expect(pollMock).toHaveBeenCalledWith(
      "session-1",
      "drill_opening_boundary",
      { boundaryToken: "a".repeat(64) },
    );
    unmount();
    expect(abortMock).toHaveBeenCalledWith(
      "session-1",
      "a".repeat(64),
    );
  });

  it("recovers an already-durable marker when the hook remounts", () => {
    snapshot = {
      sessionId: "session-1",
      openingMiddlePly: 17,
      reconciliationToken: "b".repeat(64),
      transitionRevision: 4,
    };
    revision = 4;

    const first = renderHook(() =>
      useOpeningBoundaryDelta(source, "session-1", false),
    );
    expect(pollMock).toHaveBeenCalledTimes(1);
    first.unmount();

    const second = renderHook(() =>
      useOpeningBoundaryDelta(source, "session-1", false),
    );
    expect(pollMock).toHaveBeenCalledTimes(2);
    expect(pollMock).toHaveBeenLastCalledWith(
      "session-1",
      "game_opening_boundary",
      { boundaryToken: "b".repeat(64) },
    );
    second.unmount();
  });

  it("aborts the exact owner when marker retraction or unknown revision removes the snapshot", () => {
    snapshot = {
      sessionId: "session-1",
      openingMiddlePly: 17,
      reconciliationToken: "c".repeat(64),
      transitionRevision: 1,
    };
    revision = 1;
    useGameStore.setState({
      sessionId: "session-1",
      isGameActive: true,
      openingScoreDelta: {
        sessionId: "session-1",
        items: null,
        freshness: "pending",
        source: "opening_boundary",
        reconciliationToken: "c".repeat(64),
      },
    });
    renderHook(() => useOpeningBoundaryDelta(source, "session-1", false));

    snapshot = null;
    revision = 2;
    act(() => {
      for (const listener of listeners) listener();
    });

    expect(abortMock).toHaveBeenCalledWith(
      "session-1",
      "c".repeat(64),
    );
    expect(pollMock).toHaveBeenCalledTimes(1);
    expect(useGameStore.getState().openingScoreDelta).toBeNull();
  });

  it("cleans up the old token and starts the replacement session token", () => {
    snapshot = {
      sessionId: "session-1",
      openingMiddlePly: 17,
      reconciliationToken: "d".repeat(64),
      transitionRevision: 1,
    };
    revision = 1;
    const { rerender } = renderHook(
      ({ sessionId }) =>
        useOpeningBoundaryDelta(source, sessionId, false),
      { initialProps: { sessionId: "session-1" } },
    );

    snapshot = {
      sessionId: "session-2",
      openingMiddlePly: 19,
      reconciliationToken: "e".repeat(64),
      transitionRevision: 2,
    };
    revision = 2;
    rerender({ sessionId: "session-2" });

    expect(abortMock).toHaveBeenCalledWith(
      "session-1",
      "d".repeat(64),
    );
    expect(pollMock).toHaveBeenLastCalledWith(
      "session-2",
      "game_opening_boundary",
      { boundaryToken: "e".repeat(64) },
    );
  });

  it.each([18, null])(
    "captures one content-free diagnostic for browser boundary %s against server boundary 17",
    (browserOpeningMiddlePly) => {
      snapshot = {
        sessionId: "session-1",
        openingMiddlePly: 17,
        reconciliationToken: "f".repeat(64),
        transitionRevision: 3,
      };
      revision = 3;
      renderHook(() =>
        useOpeningBoundaryDelta(
          source,
          "session-1",
          false,
          browserOpeningMiddlePly,
        ),
      );

      expect(captureMock).toHaveBeenCalledOnce();
      expect(captureMock).toHaveBeenCalledWith(
        "opening_boundary_client_mismatch",
        {
          browser_opening_ply: browserOpeningMiddlePly,
          server_opening_ply: 17,
          boundary_transition_revision: 3,
          is_drill_mode: false,
        },
      );

      snapshot = {
        ...snapshot!,
        reconciliationToken: "g".repeat(64),
        transitionRevision: 4,
      };
      revision = 4;
      act(() => {
        for (const listener of listeners) listener();
      });
      expect(captureMock).toHaveBeenCalledOnce();
    },
  );

  it("does not report a matching browser and server boundary", () => {
    snapshot = {
      sessionId: "session-1",
      openingMiddlePly: 17,
      reconciliationToken: "f".repeat(64),
      transitionRevision: 3,
    };
    revision = 3;

    renderHook(() =>
      useOpeningBoundaryDelta(source, "session-1", false, 17),
    );

    expect(captureMock).not.toHaveBeenCalled();
  });
});
