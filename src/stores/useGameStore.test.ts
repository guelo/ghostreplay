import { beforeEach, describe, expect, it } from "vitest";
import { useGameStore } from "./useGameStore";

describe("useGameStore sound settings", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGameStore.setState({ soundMuted: false, soundVolume: 1 });
  });

  it("setSoundMuted persists and updates state", () => {
    useGameStore.getState().setSoundMuted(true);

    expect(useGameStore.getState().soundMuted).toBe(true);
    expect(window.localStorage.getItem("ghostreplay_sound_muted")).toBe("true");
  });

  it("setSoundVolume persists and updates state", () => {
    useGameStore.getState().setSoundVolume(0.5);

    expect(useGameStore.getState().soundVolume).toBe(0.5);
    expect(window.localStorage.getItem("ghostreplay_sound_volume")).toBe("0.5");
  });

  it("stores the canonical clamped value for out-of-range input", () => {
    useGameStore.getState().setSoundVolume(2);
    expect(useGameStore.getState().soundVolume).toBe(1);

    useGameStore.getState().setSoundVolume(-1);
    expect(useGameStore.getState().soundVolume).toBe(0);
  });

  it("retains the previous volume for invalid input", () => {
    useGameStore.getState().setSoundVolume(0.6);
    useGameStore.getState().setSoundVolume(Number.NaN);
    expect(useGameStore.getState().soundVolume).toBe(0.6);
  });

  it("supports updater functions", () => {
    useGameStore.getState().setSoundMuted((prev) => !prev);
    expect(useGameStore.getState().soundMuted).toBe(true);
  });
});

describe("useGameStore opening deltas", () => {
  beforeEach(() => {
    useGameStore.setState(useGameStore.getInitialState(), true);
  });

  const item = (key: string, before: number | null, after: number) => ({
    opening_key: key,
    opening_name: key,
    opening_family: key,
    eco: null,
    depth: 3,
    before,
    after,
    delta: before == null ? null : after - before,
    is_new: before == null,
  });

  const reconcile = (
    sessionId: string,
    items: ReturnType<typeof item>[] | null,
  ) =>
    useGameStore
      .getState()
      .applyPolledOpeningDelta(
        sessionId,
        items,
        useGameStore.getState().openingDeltaPollToken,
      );

  it("stamps a terminal delta with the session that earned it", () => {
    expect(useGameStore.getState().openingScoreDelta).toBeNull();

    const changes = [item("k1", 41, 44)];
    useGameStore.getState().setTerminalOpeningDelta("s1", changes);

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items: changes,
      freshness: "pending",
      source: "terminal",
      reconciliationToken: expect.any(String),
    });
  });

  it("reconciles the current session in place", () => {
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setTerminalOpeningDelta("s1", [item("k1", 41, 41)]);

    const fresh = [item("k1", 41, 47)];
    reconcile("s1", fresh);

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items: fresh,
      freshness: "fresh",
      source: "terminal",
      reconciliationToken: expect.any(String),
    });
  });

  it.each([
    { label: "null", items: null },
    { label: "empty", items: [] },
    { label: "rounded-zero", items: [item("k1", 41.6, 42.1)] },
  ])("marks a no-change $label response fresh", ({ items }) => {
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setTerminalOpeningDelta("s1", null);

    reconcile("s1", items);

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items,
      freshness: "fresh",
      source: "terminal",
      reconciliationToken: expect.any(String),
    });
  });

  it("marks only the matching pending current session unavailable and retains warm items", () => {
    const warm = [item("k1", 41, 44)];
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setTerminalOpeningDelta("s1", warm);
    const token = useGameStore.getState().openingDeltaPollToken;

    useGameStore.getState().markOpeningDeltaUnavailable("other", token);
    expect(useGameStore.getState().openingScoreDelta?.freshness).toBe("pending");

    useGameStore.getState().markOpeningDeltaUnavailable("s1", token + 1);
    expect(useGameStore.getState().openingScoreDelta?.freshness).toBe("pending");

    useGameStore.getState().markOpeningDeltaUnavailable("s1", token);
    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items: warm,
      freshness: "unavailable",
      source: "terminal",
      reconciliationToken: expect.any(String),
    });
  });

  it("does not downgrade an already-fresh record to unavailable", () => {
    useGameStore.setState({ sessionId: "s1" });
    reconcile("s1", [item("k1", 41, 47)]);
    const token = useGameStore.getState().openingDeltaPollToken;

    useGameStore.getState().markOpeningDeltaUnavailable("s1", token);

    expect(useGameStore.getState().openingScoreDelta?.freshness).toBe("fresh");
  });

  it("fences provisional boundary writes behind source and reconciliation token", () => {
    useGameStore.setState({ sessionId: "s1", isGameActive: true });
    const pollToken = useGameStore.getState().openingDeltaPollToken;
    useGameStore
      .getState()
      .setBoundaryOpeningDeltaPending("s1", "boundary-a");
    useGameStore
      .getState()
      .applyPolledOpeningDelta(
        "s1",
        [item("k1", 41, 45)],
        pollToken,
        "opening_boundary",
        "boundary-a",
      );
    expect(useGameStore.getState().openingScoreDelta).toMatchObject({
      sessionId: "s1",
      freshness: "fresh",
      source: "opening_boundary",
      reconciliationToken: "boundary-a",
    });

    useGameStore
      .getState()
      .setTerminalOpeningDelta("s1", [item("k1", 41, 46)]);
    const terminal = useGameStore.getState().openingScoreDelta;
    useGameStore
      .getState()
      .applyPolledOpeningDelta(
        "s1",
        [item("k1", 41, 99)],
        pollToken,
        "opening_boundary",
        "boundary-a",
      );
    useGameStore
      .getState()
      .clearBoundaryOpeningDelta("s1", "boundary-a");
    expect(useGameStore.getState().openingScoreDelta).toEqual(terminal);
  });

  it.each(
    (["terminal", "opening_boundary"] as const).flatMap((source) =>
      (["no session", "empty", "pending", "fresh"] as const).map((slot) => ({
        source,
        slot,
      })),
    ),
  )("ignores a replaced $source result with a $slot current slot", ({ source, slot }) => {
    useGameStore.setState({
      sessionId: slot === "no session" ? null : "s2",
      isGameActive: true,
    });
    if (slot === "pending" || slot === "fresh") {
      if (source === "terminal") {
        useGameStore.getState().setTerminalOpeningDelta("s2", [item("k2", 10, 20)]);
      } else {
        useGameStore.getState().setBoundaryOpeningDeltaPending("s2", "boundary-b");
      }
      if (slot === "fresh") reconcile("s2", [item("k2", 10, 22)]);
    }
    const before = useGameStore.getState().openingScoreDelta;

    useGameStore.getState().applyPolledOpeningDelta(
      "s1",
      [item("k1", 41, 47)],
      useGameStore.getState().openingDeltaPollToken,
      source,
      // Match the other ownership fields to isolate the session-ID guard.
      before?.reconciliationToken ?? "old-owner",
    );

    expect(useGameStore.getState().openingScoreDelta).toBe(before);
  });

  it.each(["terminal", "opening_boundary"] as const)(
    "rejects source and reconciliation-token mismatches for a %s owner",
    (source) => {
      useGameStore.setState({ sessionId: "s1", isGameActive: true });
      const state = useGameStore.getState();
      if (source === "terminal") state.setTerminalOpeningDelta("s1", null);
      else state.setBoundaryOpeningDeltaPending("s1", "boundary-a");
      const owner = useGameStore.getState().openingScoreDelta!;
      const otherSource = source === "terminal" ? "opening_boundary" : "terminal";
      for (const [requestedSource, requestedToken] of [
        [otherSource, owner.reconciliationToken],
        [source, "wrong-token"],
      ] as const) {
        state.applyPolledOpeningDelta(
          "s1", [item("k1", 41, 99)], state.openingDeltaPollToken,
          requestedSource, requestedToken,
        );
        state.markOpeningDeltaUnavailable(
          "s1", state.openingDeltaPollToken, requestedSource, requestedToken,
        );
        expect(useGameStore.getState().openingScoreDelta).toBe(owner);
      }
    },
  );

  it("drops a commit carrying a stale poll token", () => {
    useGameStore.setState({ sessionId: "s1" });
    const staleToken = useGameStore.getState().openingDeltaPollToken;
    useGameStore.getState().abandonOpeningDeltas();
    useGameStore.getState().setTerminalOpeningDelta("s1", [item("k1", 41, 42)]);
    const before = useGameStore.getState().openingScoreDelta;

    useGameStore.getState().applyPolledOpeningDelta(
      "s1", [item("k1", 41, 47)], staleToken,
    );

    expect(useGameStore.getState().openingScoreDelta).toBe(before);
  });

  it.each(["pending", "fresh"] as const)(
    "beginSession clears %s data atomically and permits the new session's result",
    (freshness) => {
      useGameStore.setState({ sessionId: "s1" });
      const state = useGameStore.getState();
      state.setTerminalOpeningDelta("s1", [item("k1", 41, 47)]);
      if (freshness === "fresh") reconcile("s1", [item("k1", 41, 48)]);
      const transitions: unknown[] = [];
      const unsubscribe = useGameStore.subscribe((next) => transitions.push({
        sessionId: next.sessionId,
        moveLineRevision: next.moveLineRevision,
        openingScoreDelta: next.openingScoreDelta,
      }));

      state.beginSession("s2", 3);
      unsubscribe();

      expect(transitions).toEqual([{
        sessionId: "s2", moveLineRevision: 3, openingScoreDelta: null,
      }]);
      expect(useGameStore.getState().openingDeltaPollToken).toBe(state.openingDeltaPollToken);
      reconcile("s2", [item("k2", 10, 20)]);
      expect(useGameStore.getState().openingScoreDelta).toMatchObject({
        sessionId: "s2", freshness: "fresh", items: [item("k2", 10, 20)],
      });
    },
  );

  it("clearOpeningDelta clears the current slot", () => {
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setTerminalOpeningDelta("s1", [item("k1", 41, 47)]);

    useGameStore.getState().clearOpeningDelta();

    expect(useGameStore.getState().openingScoreDelta).toBeNull();
  });

  it("abandonOpeningDeltas clears the slot and invalidates in-flight polls", () => {
    useGameStore.setState({ sessionId: "s1" });
    reconcile("s1", [item("k1", 41, 47)]);
    const token = useGameStore.getState().openingDeltaPollToken;

    useGameStore.getState().abandonOpeningDeltas();

    expect(useGameStore.getState().openingScoreDelta).toBeNull();
    expect(useGameStore.getState().openingDeltaPollToken).toBe(token + 1);
    useGameStore.getState().beginSession("s2");
    expect(useGameStore.getState().openingScoreDelta).toBeNull();
  });
});
