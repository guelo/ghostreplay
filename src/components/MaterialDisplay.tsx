import { memo, useMemo } from "react";
import {
  PIECE_ORDER,
  PIECE_VALUES,
  parseMaterial,
} from "./MaterialDisplay.helpers";

type MaterialDisplayProps = {
  fen: string;
  perspective: "white" | "black";
};

// Unicode chess pieces: white captures black pieces (shown in black), black captures white pieces (shown in white)
const PIECE_CHARS: Record<string, { w: string; b: string }> = {
  p: { w: "♙", b: "♟" },
  n: { w: "♘", b: "♞" },
  b: { w: "♗", b: "♝" },
  r: { w: "♖", b: "♜" },
  q: { w: "♕", b: "♛" },
};

const MaterialDisplay = ({ fen, perspective }: MaterialDisplayProps) => {
  const { icons, score } = useMemo(() => {
    const { capturedByWhite, capturedByBlack } = parseMaterial(fen);
    const myCaptured =
      perspective === "white" ? capturedByWhite : capturedByBlack;
    const theirCaptured =
      perspective === "white" ? capturedByBlack : capturedByWhite;

    // Compute overall net material advantage across all piece types
    let netTotal = 0;
    for (const piece of PIECE_ORDER) {
      netTotal +=
        (myCaptured[piece] - theirCaptured[piece]) * PIECE_VALUES[piece];
    }

    // Build icon list from per-type surpluses (shown even when behind)
    const iconList: string[] = [];
    const capturedColor = perspective === "white" ? "b" : "w";
    for (const piece of PIECE_ORDER) {
      const net = myCaptured[piece] - theirCaptured[piece];
      if (net > 0) {
        const char = PIECE_CHARS[piece][capturedColor];
        for (let i = 0; i < net; i++) {
          iconList.push(char);
        }
      }
    }

    // Only show score number if this side is ahead overall
    return { icons: iconList, score: netTotal > 0 ? netTotal : 0 };
  }, [fen, perspective]);

  return (
    <div className="material-display">
      {icons.length > 0 && (
        <span className="material-icons">
          {icons.map((icon, idx) => (
            <span key={idx}>{icon}</span>
          ))}
        </span>
      )}
      {score > 0 && <span className="material-score">+{score}</span>}
    </div>
  );
};

export default memo(MaterialDisplay);
