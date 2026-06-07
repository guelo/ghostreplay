import { describe, expect, it } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useMediaQuery } from "./useMediaQuery";
import { setMatchMedia } from "../test/setup";

describe("useMediaQuery", () => {
  it("returns the initial match state", () => {
    setMatchMedia("(max-width: 500px)", true);
    const { result } = renderHook(() => useMediaQuery("(max-width: 500px)"));
    expect(result.current).toBe(true);
  });

  it("reacts to query changes", () => {
    setMatchMedia("(max-width: 600px)", false);
    const { result } = renderHook(() => useMediaQuery("(max-width: 600px)"));
    expect(result.current).toBe(false);
    act(() => {
      setMatchMedia("(max-width: 600px)", true);
    });
    expect(result.current).toBe(true);
  });
});
