import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import TimeRangeSlider from "./TimeRangeSlider";

function getTrack() {
  return document.querySelector(".time-slider__track") as HTMLElement;
}

function getHandles() {
  return screen.getAllByRole("slider");
}

function getActive() {
  return document.querySelector(".time-slider__active") as HTMLElement;
}

// Mock getBoundingClientRect for the track to have a known width
function mockTrackRect(track: HTMLElement, width = 400) {
  vi.spyOn(track, "getBoundingClientRect").mockReturnValue({
    width,
    height: 28,
    top: 0,
    left: 0,
    right: width,
    bottom: 28,
    x: 0,
    y: 0,
    toJSON: () => {},
  });
}

describe("TimeRangeSlider", () => {
  const defaultProps = {
    value: [0, 1] as [number, number],
    onChange: vi.fn(),
    paddingLeft: 52,
    paddingRight: 12,
    minFraction: 0.1,
  };

  it("renders handles at correct positions for [0, 1]", () => {
    render(<TimeRangeSlider {...defaultProps} />);

    const handles = getHandles();
    expect(handles).toHaveLength(2);
    expect(handles[0].style.left).toBe("0%");
    expect(handles[1].style.left).toBe("100%");
  });

  it("renders handles at correct positions for [0.25, 0.75]", () => {
    render(
      <TimeRangeSlider {...defaultProps} value={[0.25, 0.75]} />,
    );

    const handles = getHandles();
    expect(handles[0].style.left).toBe("25%");
    expect(handles[1].style.left).toBe("75%");
  });

  it("renders active region with correct width", () => {
    render(
      <TimeRangeSlider {...defaultProps} value={[0.25, 0.75]} />,
    );

    const active = getActive();
    expect(active.style.left).toBe("25%");
    expect(active.style.width).toBe("50%");
  });

  it("calls onChange when left handle is dragged right", () => {
    const onChange = vi.fn();
    render(
      <TimeRangeSlider
        {...defaultProps}
        value={[0, 1]}
        onChange={onChange}
      />,
    );

    const track = getTrack();
    mockTrackRect(track, 400);

    const leftHandle = getHandles()[0];

    // Drag right by 100px = 0.25 of 400px track
    fireEvent.pointerDown(leftHandle, { clientX: 0, pointerId: 1 });
    fireEvent.pointerMove(track.parentElement!, { clientX: 100, pointerId: 1 });

    expect(onChange).toHaveBeenCalled();
    const [newLeft, newRight] = onChange.mock.calls.at(-1)![0];
    expect(newLeft).toBeCloseTo(0.25, 2);
    expect(newRight).toBe(1);
  });

  it("calls onChange when right handle is dragged left", () => {
    const onChange = vi.fn();
    render(
      <TimeRangeSlider
        {...defaultProps}
        value={[0, 1]}
        onChange={onChange}
      />,
    );

    const track = getTrack();
    mockTrackRect(track, 400);

    const rightHandle = getHandles()[1];

    // Drag left by 100px from starting position
    fireEvent.pointerDown(rightHandle, { clientX: 400, pointerId: 1 });
    fireEvent.pointerMove(track.parentElement!, {
      clientX: 300,
      pointerId: 1,
    });

    expect(onChange).toHaveBeenCalled();
    const [newLeft, newRight] = onChange.mock.calls.at(-1)![0];
    expect(newLeft).toBe(0);
    expect(newRight).toBeCloseTo(0.75, 2);
  });

  it("pans both handles equally when active region is dragged", () => {
    const onChange = vi.fn();
    render(
      <TimeRangeSlider
        {...defaultProps}
        value={[0.25, 0.75]}
        onChange={onChange}
      />,
    );

    const track = getTrack();
    mockTrackRect(track, 400);

    const active = getActive();

    // Drag right by 40px = 0.1 of 400px
    fireEvent.pointerDown(active, { clientX: 200, pointerId: 1 });
    fireEvent.pointerMove(track.parentElement!, {
      clientX: 240,
      pointerId: 1,
    });

    expect(onChange).toHaveBeenCalled();
    const [newLeft, newRight] = onChange.mock.calls.at(-1)![0];
    expect(newLeft).toBeCloseTo(0.35, 2);
    expect(newRight).toBeCloseTo(0.85, 2);
  });

  it("clamps pan at right boundary", () => {
    const onChange = vi.fn();
    render(
      <TimeRangeSlider
        {...defaultProps}
        value={[0.5, 0.8]}
        onChange={onChange}
      />,
    );

    const track = getTrack();
    mockTrackRect(track, 400);

    const active = getActive();

    // Drag far right - should clamp
    fireEvent.pointerDown(active, { clientX: 200, pointerId: 1 });
    fireEvent.pointerMove(track.parentElement!, {
      clientX: 600,
      pointerId: 1,
    });

    expect(onChange).toHaveBeenCalled();
    const [newLeft, newRight] = onChange.mock.calls.at(-1)![0];
    expect(newRight).toBe(1);
    expect(newLeft).toBeCloseTo(0.7, 2); // span 0.3 preserved
  });

  it("enforces minFraction — left handle cannot cross past right", () => {
    const onChange = vi.fn();
    render(
      <TimeRangeSlider
        {...defaultProps}
        value={[0.4, 0.5]}
        onChange={onChange}
        minFraction={0.1}
      />,
    );

    const track = getTrack();
    mockTrackRect(track, 400);

    const leftHandle = getHandles()[0];

    // Try to drag left handle past right handle
    fireEvent.pointerDown(leftHandle, { clientX: 160, pointerId: 1 });
    fireEvent.pointerMove(track.parentElement!, {
      clientX: 360,
      pointerId: 1,
    });

    expect(onChange).toHaveBeenCalled();
    const [newLeft, newRight] = onChange.mock.calls.at(-1)![0];
    // Left should be clamped to right - minFraction
    expect(newLeft).toBe(newRight - 0.1);
    expect(newRight).toBe(0.5);
  });

  it("clears drag state on pointerUp", () => {
    const onChange = vi.fn();
    render(
      <TimeRangeSlider
        {...defaultProps}
        value={[0, 1]}
        onChange={onChange}
      />,
    );

    const track = getTrack();
    mockTrackRect(track, 400);

    const leftHandle = getHandles()[0];

    fireEvent.pointerDown(leftHandle, { clientX: 0, pointerId: 1 });
    fireEvent.pointerUp(track.parentElement!, { pointerId: 1 });

    // Moving after pointerUp should not trigger onChange
    onChange.mockClear();
    fireEvent.pointerMove(track.parentElement!, { clientX: 100, pointerId: 1 });
    expect(onChange).not.toHaveBeenCalled();
  });

  describe("keyboard interaction", () => {
    it("moves left handle right on ArrowRight", () => {
      const onChange = vi.fn();
      render(
        <TimeRangeSlider
          {...defaultProps}
          value={[0.3, 0.8]}
          onChange={onChange}
        />,
      );

      const leftHandle = getHandles()[0];
      fireEvent.keyDown(leftHandle, { key: "ArrowRight" });

      expect(onChange).toHaveBeenCalledTimes(1);
      const [newLeft, newRight] = onChange.mock.calls[0][0];
      expect(newLeft).toBeCloseTo(0.32, 2); // 0.3 + 0.02 step
      expect(newRight).toBe(0.8);
    });

    it("moves right handle left on ArrowLeft", () => {
      const onChange = vi.fn();
      render(
        <TimeRangeSlider
          {...defaultProps}
          value={[0.3, 0.8]}
          onChange={onChange}
        />,
      );

      const rightHandle = getHandles()[1];
      fireEvent.keyDown(rightHandle, { key: "ArrowLeft" });

      expect(onChange).toHaveBeenCalledTimes(1);
      const [newLeft, newRight] = onChange.mock.calls[0][0];
      expect(newLeft).toBe(0.3);
      expect(newRight).toBeCloseTo(0.78, 2); // 0.8 - 0.02 step
    });

    it("uses large step with Shift held", () => {
      const onChange = vi.fn();
      render(
        <TimeRangeSlider
          {...defaultProps}
          value={[0.3, 0.8]}
          onChange={onChange}
        />,
      );

      const leftHandle = getHandles()[0];
      fireEvent.keyDown(leftHandle, { key: "ArrowRight", shiftKey: true });

      expect(onChange).toHaveBeenCalledTimes(1);
      const [newLeft] = onChange.mock.calls[0][0];
      expect(newLeft).toBeCloseTo(0.4, 2); // 0.3 + 0.1 large step
    });

    it("clamps handle at minFraction from other handle", () => {
      const onChange = vi.fn();
      render(
        <TimeRangeSlider
          {...defaultProps}
          value={[0.45, 0.5]}
          onChange={onChange}
          minFraction={0.1}
        />,
      );

      const leftHandle = getHandles()[0];
      fireEvent.keyDown(leftHandle, { key: "ArrowRight" });

      expect(onChange).toHaveBeenCalledTimes(1);
      const [newLeft, newRight] = onChange.mock.calls[0][0];
      // 0.45 + 0.02 = 0.47, but clamped to right - minFraction = 0.4
      expect(newLeft).toBe(newRight - 0.1);
    });

    it("Home key moves handle to minimum position", () => {
      const onChange = vi.fn();
      render(
        <TimeRangeSlider
          {...defaultProps}
          value={[0.5, 0.8]}
          onChange={onChange}
        />,
      );

      const leftHandle = getHandles()[0];
      fireEvent.keyDown(leftHandle, { key: "Home" });

      expect(onChange).toHaveBeenCalledTimes(1);
      const [newLeft] = onChange.mock.calls[0][0];
      expect(newLeft).toBe(0);
    });

    it("End key moves handle to maximum position", () => {
      const onChange = vi.fn();
      render(
        <TimeRangeSlider
          {...defaultProps}
          value={[0.2, 0.7]}
          onChange={onChange}
        />,
      );

      const rightHandle = getHandles()[1];
      fireEvent.keyDown(rightHandle, { key: "End" });

      expect(onChange).toHaveBeenCalledTimes(1);
      const [, newRight] = onChange.mock.calls[0][0];
      expect(newRight).toBe(1);
    });
  });
});
