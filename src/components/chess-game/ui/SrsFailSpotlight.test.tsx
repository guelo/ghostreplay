import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "../../../test/utils";
import { createRef } from "react";
import { fireEvent } from "@testing-library/react";
import SrsFailSpotlight, {
  SRS_FAIL_HOLD_MS,
  SRS_FAIL_SHRINK_MS,
} from "./SrsFailSpotlight";

function makeTargetRef() {
  const el = document.createElement("div");
  el.getBoundingClientRect = () =>
    ({ left: 100, top: 100, right: 500, bottom: 500, width: 400, height: 400, x: 100, y: 100, toJSON: () => ({}) }) as DOMRect;
  const ref = createRef<HTMLElement>();
  (ref as { current: HTMLElement | null }).current = el;
  return ref;
}

describe("SrsFailSpotlight", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders scrim + content while active and unmounts after the lifecycle", () => {
    const onDone = vi.fn();
    render(
      <SrsFailSpotlight
        trigger={{ id: 1, moveIndex: 3 }}
        targetRef={makeTargetRef()}
        onDone={onDone}
      />,
    );

    expect(document.querySelector(".srs-fail-scrim")).not.toBeNull();
    expect(document.querySelector(".srs-fail-content")).not.toBeNull();

    act(() => {
      vi.advanceTimersByTime(SRS_FAIL_HOLD_MS + 10);
    });
    act(() => {
      vi.advanceTimersByTime(SRS_FAIL_SHRINK_MS + 50);
    });

    expect(document.querySelector(".srs-fail-scrim")).toBeNull();
    expect(onDone).toHaveBeenCalledWith(1);
  });

  it("skips to shrink early when the scrim is clicked", () => {
    const onDone = vi.fn();
    render(
      <SrsFailSpotlight
        trigger={{ id: 2, moveIndex: 0 }}
        targetRef={makeTargetRef()}
        onDone={onDone}
      />,
    );

    const scrim = document.querySelector(".srs-fail-scrim") as HTMLElement;
    act(() => {
      fireEvent.click(scrim);
    });
    expect(document.querySelector(".srs-fail-scrim--shrink")).not.toBeNull();
    expect(onDone).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(SRS_FAIL_SHRINK_MS + 20);
    });
    expect(onDone).toHaveBeenCalledWith(2);
  });

  it("renders nothing without a trigger", () => {
    render(
      <SrsFailSpotlight trigger={null} targetRef={makeTargetRef()} onDone={vi.fn()} />,
    );
    expect(document.querySelector(".srs-fail-scrim")).toBeNull();
  });
});
