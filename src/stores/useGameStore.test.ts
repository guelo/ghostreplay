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

describe("useGameStore opening deltas (g-f3m4)", () => {
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
    });
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it.each([null, []])("marks a no-change %s response fresh", (items) => {
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setTerminalOpeningDelta("s1", null);

    reconcile("s1", items);

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items,
      freshness: "fresh",
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
    });
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("does not downgrade an already-fresh record to unavailable", () => {
    useGameStore.setState({ sessionId: "s1" });
    reconcile("s1", [item("k1", 41, 47)]);
    const token = useGameStore.getState().openingDeltaPollToken;

    useGameStore.getState().markOpeningDeltaUnavailable("s1", token);

    expect(useGameStore.getState().openingScoreDelta?.freshness).toBe("fresh");
  });

  it("queues a superseded session's delta instead of dropping it", () => {
    useGameStore.setState({ sessionId: "s2" });
    const fresh = [item("k1", 41, 47)];

    reconcile("s1", fresh);

    const state = useGameStore.getState();
    // Never contaminates the current drill's inline slot.
    expect(state.openingScoreDelta).toBeNull();
    expect(state.lateOpeningDeltas).toHaveLength(1);
    expect(state.lateOpeningDeltas[0]).toMatchObject({ sessionId: "s1", items: fresh });
  });

  it("drops a commit carrying a stale poll token", () => {
    useGameStore.setState({ sessionId: "s1" });
    const staleToken = useGameStore.getState().openingDeltaPollToken;
    useGameStore.getState().abandonOpeningDeltas(); // bumps the token

    useGameStore
      .getState()
      .applyPolledOpeningDelta("s1", [item("k1", 41, 47)], staleToken);

    expect(useGameStore.getState().openingScoreDelta).toBeNull();
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("never queues an unrenderable delta, so it cannot block the head", () => {
    useGameStore.setState({ sessionId: "s2" });

    reconcile("s1", []);
    reconcile("s1", [item("k1", 44, 44)]); // rounds to a zero diff -> no badge

    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("queues a departing session's delta rather than filling an unseen slot", () => {
    // The player clicked "Again"; the end screen is gone but /start has not
    // resolved, so sessionId is still s1. Committing inline here would render to
    // nobody and then be cleared by beginSession.
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setDepartingSession("s1");
    const fresh = [item("k1", 41, 47)];

    reconcile("s1", fresh);

    const state = useGameStore.getState();
    expect(state.openingScoreDelta).toBeNull();
    expect(state.lateOpeningDeltas).toHaveLength(1);
    expect(state.lateOpeningDeltas[0]).toMatchObject({ sessionId: "s1" });
  });

  it("does not replay an already-visible delta as a late toast", () => {
    // Reconciled while s1's end screen was up -> the player SAW the badges.
    // Promoting it on the next session start would show the same numbers twice.
    useGameStore.setState({ sessionId: "s1" });
    reconcile("s1", [item("k1", 41, 47)]);
    expect(useGameStore.getState().openingScoreDelta).not.toBeNull();

    useGameStore.getState().beginSession("s2");

    const state = useGameStore.getState();
    expect(state.sessionId).toBe("s2");
    expect(state.openingScoreDelta).toBeNull();
    expect(state.lateOpeningDeltas).toEqual([]);
  });

  it("beginSession clears the departure mark", () => {
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setDepartingSession("s1");

    useGameStore.getState().beginSession("s2");
    expect(useGameStore.getState().departingSessionId).toBeNull();

    // ...so s2's own reconciliation lands inline as usual.
    reconcile("s2", [item("k2", 10, 20)]);
    expect(useGameStore.getState().openingScoreDelta?.sessionId).toBe("s2");
  });

  it("setDepartingSession(null) restores inline delivery after a failed start", () => {
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setDepartingSession("s1");
    useGameStore.getState().setDepartingSession(null);

    reconcile("s1", [item("k1", 41, 47)]);

    expect(useGameStore.getState().openingScoreDelta?.sessionId).toBe("s1");
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("beginSession discards an unreconciled terminal delta", () => {
    // A warm terminal delta is commonly equal to the stored before-score, so
    // promoting it would surface the same suppressed zero-diff as a toast.
    useGameStore.setState({ sessionId: "s1" });
    useGameStore.getState().setTerminalOpeningDelta("s1", [item("k1", 41, 47)]);

    useGameStore.getState().beginSession("s2");

    expect(useGameStore.getState().openingScoreDelta).toBeNull();
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("clearOpeningDelta clears the current slot but leaves the queue intact", () => {
    useGameStore.setState({ sessionId: "s2" });
    reconcile("s1", [item("k1", 41, 47)]);
    useGameStore.getState().setTerminalOpeningDelta("s2", [item("k2", 10, 20)]);

    useGameStore.getState().clearOpeningDelta();

    expect(useGameStore.getState().openingScoreDelta).toBeNull();
    expect(useGameStore.getState().lateOpeningDeltas).toHaveLength(1);
  });

  it("abandonOpeningDeltas clears both slots without promoting", () => {
    useGameStore.setState({ sessionId: "s1" });
    reconcile("s1", [item("k1", 41, 47)]);

    useGameStore.getState().abandonOpeningDeltas();

    expect(useGameStore.getState().openingScoreDelta).toBeNull();
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
    // ...and stays empty across the next session start.
    useGameStore.getState().beginSession("s2");
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("caps the queue at 3, dropping the oldest", () => {
    useGameStore.setState({ sessionId: "current" });
    for (const id of ["a", "b", "c", "d"]) {
      reconcile(id, [item(id, 41, 47)]);
    }

    const queued = useGameStore.getState().lateOpeningDeltas;
    expect(queued).toHaveLength(3);
    expect(queued.map((d) => d.sessionId)).toEqual(["b", "c", "d"]);
  });

  it("acknowledges by nonce, leaving an unshown duplicate of the same session", () => {
    useGameStore.setState({ sessionId: "current" });
    reconcile("s1", [item("k1", 41, 47)]);
    reconcile("s1", [item("k1", 47, 52)]);

    const [first, second] = useGameStore.getState().lateOpeningDeltas;
    expect(first.nonce).not.toBe(second.nonce);

    useGameStore.getState().acknowledgeLateOpeningDelta(first.nonce);

    // Acking by sessionId would have removed the later duplicate too.
    const remaining = useGameStore.getState().lateOpeningDeltas;
    expect(remaining).toHaveLength(1);
    expect(remaining[0].nonce).toBe(second.nonce);
  });
});
