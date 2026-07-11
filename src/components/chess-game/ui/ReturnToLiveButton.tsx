import { memo, useEffect, useState } from "react";
import type { CSSProperties, RefObject } from "react";

const COMPACT_BOARD_WIDTH_PX = 416;
const TUCK_DELAY_MS = 2_000;

type ReturnToLiveButtonProps = {
  boardRef: RefObject<HTMLDivElement | null>;
  onReturnToLive: () => void;
  suppressed: boolean;
};

type ReturnLiveStyle = CSSProperties & {
  "--board-return-live-size"?: string;
  "--board-return-live-height"?: string;
};

const ReturnToLiveButton = ({
  boardRef,
  onReturnToLive,
  suppressed,
}: ReturnToLiveButtonProps) => {
  const [compact, setCompact] = useState(false);
  const [tucked, setTucked] = useState(false);
  const [compactSize, setCompactSize] = useState("2.5rem");
  const [compactHeight, setCompactHeight] = useState("1.25rem");

  useEffect(() => {
    const board = boardRef.current;
    if (!board) return;

    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      const boardWidth = entry.contentRect.width;
      const nextCompact = boardWidth < COMPACT_BOARD_WIDTH_PX;

      setCompact(nextCompact);
      const nextCompactSize = Math.max(32, Math.min(47, boardWidth * 0.1125));
      setCompactSize(`${nextCompactSize}px`);
      setCompactHeight(`${nextCompactSize / 2}px`);

      if (
        nextCompact &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ) {
        setTucked(true);
      }
    });

    observer.observe(board);
    return () => observer.disconnect();
  }, [boardRef]);

  useEffect(() => {
    if (!compact || tucked) return;
    const timer = window.setTimeout(() => setTucked(true), TUCK_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [compact, tucked]);

  if (suppressed) return null;

  const isTucked = compact && tucked;
  const style: ReturnLiveStyle = {
    "--board-return-live-size": compactSize,
    "--board-return-live-height": compactHeight,
  };

  return (
    <button
      type="button"
      className={`board-return-live${isTucked ? " board-return-live--tucked" : ""}`}
      style={style}
      aria-label="Return to live"
      title="Return to live"
      onClick={onReturnToLive}
    >
      <span className="board-return-live__glyph" aria-hidden="true">
        ⟩⟩
      </span>
      <span className="board-return-live__label">Return to live</span>
    </button>
  );
};

export default memo(ReturnToLiveButton);
