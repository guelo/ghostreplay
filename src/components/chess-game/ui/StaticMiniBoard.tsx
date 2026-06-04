import { memo } from "react";

type BoardOrientation = "white" | "black";

type StaticMiniBoardProps = {
  fen: string;
  orientation?: BoardOrientation;
};

// Use the filled (solid) glyph set for both colors so white pieces render as a
// solid white fill (colored via CSS) rather than the hard-to-see outline glyphs.
const PIECE_GLYPHS: Record<string, string> = {
  K: "♚",
  Q: "♛",
  R: "♜",
  B: "♝",
  N: "♞",
  P: "♟",
  k: "♚",
  q: "♛",
  r: "♜",
  b: "♝",
  n: "♞",
  p: "♟",
};

/**
 * Lightweight static rendering of a FEN position. Unlike react-chessboard this
 * is a pure CSS grid with no drag handlers, animation, or measurement work, so
 * mounting it inside the live game view adds negligible rendering cost.
 */
function parseFenRanks(fen: string): string[][] {
  const placement = fen.split(" ")[0] ?? "";
  return placement.split("/").map((rank) => {
    const squares: string[] = [];
    for (const ch of rank) {
      if (ch >= "1" && ch <= "8") {
        for (let i = 0; i < Number(ch); i += 1) squares.push("");
      } else {
        squares.push(ch);
      }
    }
    return squares;
  });
}

function StaticMiniBoard({ fen, orientation = "white" }: StaticMiniBoardProps) {
  const ranks = parseFenRanks(fen);
  const orderedRanks = orientation === "white" ? ranks : [...ranks].reverse();
  const squares: { piece: string; dark: boolean; key: string }[] = [];

  orderedRanks.forEach((rank, rankIndex) => {
    const orderedFiles = orientation === "white" ? rank : [...rank].reverse();
    orderedFiles.forEach((piece, fileIndex) => {
      // Original board coordinates so colors stay correct under either orientation.
      const boardRank = orientation === "white" ? rankIndex : 7 - rankIndex;
      const boardFile = orientation === "white" ? fileIndex : 7 - fileIndex;
      squares.push({
        piece,
        dark: (boardRank + boardFile) % 2 === 1,
        key: `${rankIndex}-${fileIndex}`,
      });
    });
  });

  return (
    <div
      className="static-mini-board"
      data-testid="ghost-board"
      data-position={fen}
      data-orientation={orientation}
      role="img"
      aria-label="Ghost target blunder position"
    >
      {squares.map(({ piece, dark, key }) => (
        <div
          key={key}
          className={`static-mini-board__sq${dark ? " static-mini-board__sq--dark" : ""}`}
        >
          {piece &&
            (() => {
              const glyph = PIECE_GLYPHS[piece] ?? "";
              return (
                <span
                  className={`static-mini-board__piece${
                    piece === piece.toLowerCase()
                      ? " static-mini-board__piece--black"
                      : " static-mini-board__piece--white"
                  }`}
                  data-glyph={glyph}
                >
                  {glyph}
                </span>
              );
            })()}
        </div>
      ))}
    </div>
  );
}

export default memo(StaticMiniBoard);
