import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useBoardNotice } from "./useBoardNotice";
import type { ResolvedReview } from "../components/chess-game/types";

type Args = Parameters<typeof useBoardNotice>[0];

const baseArgs: Args = {
  isReviewMomentActive: false,
  resolvedReview: null,
  showRehookNotice: false,
  isViewingLive: true,
};

const pass = (analysisId: string): ResolvedReview => ({
  analysisId,
  moveIndex: 2,
  result: "pass",
});
const fail = (analysisId: string): ResolvedReview => ({
  analysisId,
  moveIndex: 2,
  result: "fail",
});
const pending = (analysisId: string): ResolvedReview => ({
  analysisId,
  moveIndex: 2,
  result: "pending",
});

describe("useBoardNotice", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows a review warning on the rising edge and auto-dismisses at 4s", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });
    expect(result.current).toBeNull();

    rerender({ ...baseArgs, isReviewMomentActive: true });
    expect(result.current?.kind).toBe("review-warning");

    act(() => {
      vi.advanceTimersByTime(3999);
    });
    expect(result.current?.kind).toBe("review-warning");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBeNull();
  });

  it("re-fires the warning when the review moment is re-entered", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, isReviewMomentActive: true });
    const firstNonce = result.current?.nonce;
    expect(result.current?.kind).toBe("review-warning");

    // Leave and re-enter the FEN.
    rerender({ ...baseArgs, isReviewMomentActive: false });
    rerender({ ...baseArgs, isReviewMomentActive: true });
    expect(result.current?.kind).toBe("review-warning");
    expect(result.current?.nonce).not.toBe(firstNonce);
  });

  it("clears a showing warning immediately when the review moment ends", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, isReviewMomentActive: true });
    expect(result.current?.kind).toBe("review-warning");

    // Player made the review move: isReviewMomentActive falls before the 4s timer.
    rerender({ ...baseArgs, isReviewMomentActive: false });
    expect(result.current).toBeNull();
  });

  it("shows nothing for a pending review and clears any showing warning", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, isReviewMomentActive: true });
    expect(result.current?.kind).toBe("review-warning");

    // The commit that flips resolvedReview to pending also drops the moment.
    rerender({
      ...baseArgs,
      isReviewMomentActive: false,
      resolvedReview: pending("a1"),
    });
    expect(result.current).toBeNull();
  });

  it("shows a pass result and auto-dismisses at 2s", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, resolvedReview: pending("a1") });
    expect(result.current).toBeNull();

    rerender({ ...baseArgs, resolvedReview: pass("a1") });
    expect(result.current).toEqual(
      expect.objectContaining({ kind: "review-result", result: "pass" }),
    );

    act(() => {
      vi.advanceTimersByTime(1999);
    });
    expect(result.current?.kind).toBe("review-result");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBeNull();
  });

  it("shows a fail result", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, resolvedReview: fail("a1") });
    expect(result.current).toEqual(
      expect.objectContaining({ kind: "review-result", result: "fail" }),
    );
  });

  it("shows a fail result when the spotlight scrubs the board in the same commit", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    // Player makes the review move while live → pending.
    rerender({ ...baseArgs, isViewingLive: true, resolvedReview: pending("a1") });
    expect(result.current).toBeNull();

    // The grade and the SRS-fail spotlight's scrub land together: resolvedReview
    // flips to fail and isViewingLive flips false in one render. The ✗ box must
    // still appear (the prior render was live).
    rerender({ ...baseArgs, isViewingLive: false, resolvedReview: fail("a1") });
    expect(result.current).toEqual(
      expect.objectContaining({ kind: "review-result", result: "fail" }),
    );
  });

  it("lets a new review warning preempt a showing result", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, resolvedReview: pass("a1") });
    expect(result.current?.kind).toBe("review-result");

    // A back-to-back review fires a new warning while the result is still up.
    rerender({
      ...baseArgs,
      resolvedReview: pass("a1"),
      isReviewMomentActive: true,
    });
    expect(result.current?.kind).toBe("review-warning");
  });

  it("never lets rehook stomp a showing review notice", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, isReviewMomentActive: true });
    expect(result.current?.kind).toBe("review-warning");

    rerender({
      ...baseArgs,
      isReviewMomentActive: true,
      showRehookNotice: true,
    });
    expect(result.current?.kind).toBe("review-warning");
  });

  it("shows a rehook notice and auto-dismisses at 3s", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, showRehookNotice: true });
    expect(result.current?.kind).toBe("rehook");

    act(() => {
      vi.advanceTimersByTime(2999);
    });
    expect(result.current?.kind).toBe("rehook");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBeNull();
  });

  it("never shows a result graded while scrubbed, even after returning to live", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: { ...baseArgs, isViewingLive: false },
    });

    // Result lands while viewing history.
    rerender({ ...baseArgs, isViewingLive: false, resolvedReview: pass("a1") });
    expect(result.current).toBeNull();

    // Returning to live must not replay the missed result.
    rerender({ ...baseArgs, isViewingLive: true, resolvedReview: pass("a1") });
    expect(result.current).toBeNull();
  });

  it("clears the active notice when scrubbing off live", () => {
    const { result, rerender } = renderHook((args: Args) => useBoardNotice(args), {
      initialProps: baseArgs,
    });

    rerender({ ...baseArgs, showRehookNotice: true });
    expect(result.current?.kind).toBe("rehook");

    rerender({ ...baseArgs, showRehookNotice: true, isViewingLive: false });
    expect(result.current).toBeNull();
  });
});
