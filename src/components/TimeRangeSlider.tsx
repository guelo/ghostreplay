import { useRef, useState, useCallback } from "react";

export interface TimeRangeSliderProps {
  value: [number, number];
  onChange: (value: [number, number]) => void;
  paddingLeft: number;
  paddingRight: number;
  minFraction: number;
}

type DragTarget = "left" | "right" | "pan";

const KEY_STEP = 0.02;
const KEY_STEP_LARGE = 0.1;

function clamp(v: number, min: number, max: number) {
  return Math.min(max, Math.max(min, v));
}

export default function TimeRangeSlider({
  value,
  onChange,
  paddingLeft,
  paddingRight,
  minFraction,
}: TimeRangeSliderProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    target: DragTarget;
    startX: number;
    startValue: [number, number];
  } | null>(null);
  const [dragging, setDragging] = useState(false);

  const [left, right] = value;
  const leftPct = `${left * 100}%`;
  const rightPct = `${right * 100}%`;
  const activePct = `${(right - left) * 100}%`;

  const handlePointerDown = useCallback(
    (target: DragTarget) => (e: React.PointerEvent) => {
      e.preventDefault();
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      dragRef.current = {
        target,
        startX: e.clientX,
        startValue: [value[0], value[1]],
      };
      setDragging(true);
    },
    [value],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag || !trackRef.current) return;

      const trackWidth = trackRef.current.getBoundingClientRect().width;
      if (trackWidth <= 0) return;

      const dx = (e.clientX - drag.startX) / trackWidth;
      const [s0, s1] = drag.startValue;

      if (drag.target === "left") {
        const newLeft = clamp(s0 + dx, 0, s1 - minFraction);
        onChange([newLeft, s1]);
      } else if (drag.target === "right") {
        const newRight = clamp(s1 + dx, s0 + minFraction, 1);
        onChange([s0, newRight]);
      } else {
        const span = s1 - s0;
        const newLeft = clamp(s0 + dx, 0, 1 - span);
        onChange([newLeft, newLeft + span]);
      }
    },
    [onChange, minFraction],
  );

  const handlePointerUp = useCallback(() => {
    dragRef.current = null;
    setDragging(false);
  }, []);

  const handleKeyDown = useCallback(
    (handle: "left" | "right") => (e: React.KeyboardEvent) => {
      const step = e.shiftKey ? KEY_STEP_LARGE : KEY_STEP;
      let delta = 0;

      if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
        delta = -step;
      } else if (e.key === "ArrowRight" || e.key === "ArrowUp") {
        delta = step;
      } else if (e.key === "Home") {
        delta = handle === "left" ? -left : -(right - left - minFraction);
      } else if (e.key === "End") {
        delta = handle === "left" ? right - minFraction - left : 1 - right;
      } else {
        return;
      }

      e.preventDefault();

      if (handle === "left") {
        const newLeft = clamp(left + delta, 0, right - minFraction);
        onChange([newLeft, right]);
      } else {
        const newRight = clamp(right + delta, left + minFraction, 1);
        onChange([left, newRight]);
      }
    },
    [left, right, minFraction, onChange],
  );

  return (
    <div
      className="time-slider"
      style={{ paddingLeft, paddingRight }}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
    >
      <div className="time-slider__track" ref={trackRef}>
        {/* Left inactive zone */}
        <div
          className="time-slider__inactive"
          style={{ left: 0, width: leftPct }}
        />

        {/* Left handle */}
        <div
          className="time-slider__handle"
          style={{ left: leftPct }}
          onPointerDown={handlePointerDown("left")}
          onKeyDown={handleKeyDown("left")}
          role="slider"
          aria-label="Range start"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(left * 100)}
          tabIndex={0}
        />

        {/* Active (pan) region */}
        <div
          className={`time-slider__active${dragging ? " time-slider__active--dragging" : ""}`}
          style={{ left: leftPct, width: activePct }}
          onPointerDown={handlePointerDown("pan")}
        />

        {/* Right handle */}
        <div
          className="time-slider__handle"
          style={{ left: rightPct }}
          onPointerDown={handlePointerDown("right")}
          onKeyDown={handleKeyDown("right")}
          role="slider"
          aria-label="Range end"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(right * 100)}
          tabIndex={0}
        />

        {/* Right inactive zone */}
        <div
          className="time-slider__inactive"
          style={{ left: rightPct, width: `${(1 - right) * 100}%` }}
        />
      </div>
    </div>
  );
}
