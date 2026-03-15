import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import type { AnalysisMove, SessionMoveClassification } from "../utils/api";
import { useMoveAnalysis } from "../hooks/useMoveAnalysis";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { classifyMove, toWhitePerspective } from "../workers/analysisUtils";
import type { MoveClassification } from "../workers/analysisUtils";
import AnalysisGraph from "./AnalysisGraph";
import EvalBar from "./EvalBar";
import MoveList from "./MoveList";

const STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

type AnalysisBoardProps = {
  moves: AnalysisMove[];
  boardOrientation: "white" | "black";
  startingFen?: string;
  initialMoveIndex?: number;
  footer?: React.ReactNode;
};

type WhatIfMove = {
  san: string;
  fen: string;
};

// Map API classification to MoveList's classification type
const mapClassification = (
  c: SessionMoveClassification | null,
): MoveClassification | null => {
  if (!c) return null;
  switch (c) {
    case "best":
      return "best";
    case "excellent":
      return "great";
    case "good":
      return "good";
    case "inaccuracy":
      return "inaccuracy";
    case "mistake":
      return "inaccuracy";
    case "blunder":
      return "blunder";
  }
};

// Convert SAN move to start/end squares using chess.js
const sanToSquares = (
  fen: string,
  san: string,
): { from: string; to: string } | null => {
  try {
    const tempChess = new Chess(fen);
    const result = tempChess.move(san);
    if (!result) return null;
    return { from: result.from, to: result.to };
  } catch {
    return null;
  }
};

const uciToSquares = (uci: string) => ({
  startSquare: uci.slice(0, 2),
  endSquare: uci.slice(2, 4),
});

/** Extract side-to-move from a FEN string without constructing a Chess instance. */
const fenSideToMove = (fen: string): "w" | "b" => {
  const idx = fen.indexOf(" ");
  return (idx >= 0 ? fen[idx + 1] : "w") as "w" | "b";
};

const ENGINE_ARROW_COLORS = [
  "rgba(59, 130, 246, 0.85)",
  "rgba(59, 130, 246, 0.5)",
  "rgba(59, 130, 246, 0.3)",
];

const formatEvalCp = (cp: number): string => {
  const value = cp / 100;
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}`;
};

const formatEvalValue = (
  cp: number | null,
  mate: number | null,
): string | null => {
  if (mate !== null) return `M${mate}`;
  if (cp !== null) return formatEvalCp(cp);
  return null;
};

const evalTextClass = (cp: number | null, mate: number | null): string =>
  (cp !== null && cp < 0) || (mate !== null && mate < 0)
    ? "analysis-board__eval-text--negative"
    : "analysis-board__eval-text--positive";

const formatEvalDelta = (delta: number | null): string | null => {
  if (delta === null) return null;
  return `${delta >= 0 ? "+" : ""}${delta} cp`;
};

const toWhitePerspectiveMate = (
  moverPerspectiveMate: number | null,
  moveIndex: number | null | undefined,
) => {
  if (
    moverPerspectiveMate === null ||
    moveIndex === null ||
    moveIndex === undefined
  ) {
    return moverPerspectiveMate;
  }

  return moveIndex % 2 === 0 ? moverPerspectiveMate : -moverPerspectiveMate;
};

const AnalysisBoard = ({
  moves,
  boardOrientation,
  startingFen = STARTING_FEN,
  initialMoveIndex,
  footer,
}: AnalysisBoardProps) => {
  const [currentIndex, setCurrentIndex] = useState<number | null>(
    initialMoveIndex ?? null,
  );
  const [whatIfMoves, setWhatIfMoves] = useState<WhatIfMove[]>([]);
  const [whatIfBranchPoint, setWhatIfBranchPoint] = useState(-1);
  const { analyzeMove, analysisMap, lastAnalysis, clearAnalysis } =
    useMoveAnalysis();
  const { info: engineLines, isThinking: engineThinking, evaluatePosition, stopSearch } = useStockfishEngine();
  const [showEngineArrows, setShowEngineArrows] = useState(true);
  const engineFenRef = useRef<string | null>(null);

  const isInWhatIf = whatIfMoves.length > 0;
  const effectiveIndex = currentIndex ?? moves.length - 1;

  // Map AnalysisMove[] to Move[] for MoveList
  const mappedMoves = useMemo(
    () =>
      moves.map((m, i) => ({
        san: m.move_san,
        classification: mapClassification(m.classification),
        eval: toWhitePerspective(m.eval_cp, i),
      })),
    [moves],
  );

  // Extract eval values for the graph
  const evals = useMemo(
    () => moves.map((m, i) => toWhitePerspective(m.eval_cp, i)),
    [moves],
  );

  // Combined moves for MoveList when in what-if mode
  const moveListMoves = useMemo(() => {
    if (!isInWhatIf) return mappedMoves;
    const base = mappedMoves.slice(0, whatIfBranchPoint + 1);
    const branch = whatIfMoves.map((m, i) => {
      const absIndex = whatIfBranchPoint + 1 + i;
      const analysis = analysisMap.get(absIndex);
      const prevAnalysis = analysisMap.get(absIndex - 1);
      const preEval = prevAnalysis?.playedEval ?? null;
      return {
        san: m.san,
        classification: analysis ? classifyMove(analysis.delta, preEval) : undefined,
        eval:
          analysis?.playedEval != null
            ? toWhitePerspective(analysis.playedEval, absIndex)
            : undefined,
      };
    });
    return [...base, ...branch];
  }, [isInWhatIf, mappedMoves, whatIfBranchPoint, whatIfMoves, analysisMap]);

  // Current move list index accounting for what-if
  const moveListIndex = useMemo(() => {
    if (!isInWhatIf) return currentIndex;
    // In what-if mode, navigate within the combined array
    return null; // viewing latest (end of what-if line)
  }, [isInWhatIf, currentIndex]);

  // FEN at the position before the current move (needed for arrow SAN→UCI)
  const fenBeforeCurrentMove = useMemo(() => {
    if (isInWhatIf) return null; // no arrows in what-if
    if (effectiveIndex < 0) return null;
    if (effectiveIndex === 0) return startingFen;
    return moves[effectiveIndex - 1]?.fen_after ?? startingFen;
  }, [isInWhatIf, effectiveIndex, moves, startingFen]);

  // Displayed FEN
  const displayedFen = useMemo(() => {
    if (isInWhatIf) {
      if (whatIfMoves.length === 0) return startingFen;
      return whatIfMoves[whatIfMoves.length - 1].fen;
    }
    if (effectiveIndex === -1) return startingFen;
    return moves[effectiveIndex]?.fen_after ?? startingFen;
  }, [isInWhatIf, whatIfMoves, effectiveIndex, moves, startingFen]);

  // Stop engine and clear lines synchronously when position changes
  const prevFenRef = useRef(displayedFen);
  if (prevFenRef.current !== displayedFen) {
    prevFenRef.current = displayedFen;
    stopSearch();
  }

  // Start new evaluation after render
  useEffect(() => {
    if (!displayedFen || !showEngineArrows) return;
    evaluatePosition(displayedFen, { depth: 21, multipv: 3 }).catch(() => {
      // Evaluation cancelled or engine unavailable — ignore
    });
  }, [displayedFen, evaluatePosition, showEngineArrows]);

  // Side-to-move derived from FEN (avoids constructing Chess just for turn())
  const sideToMove = useMemo(() => fenSideToMove(displayedFen), [displayedFen]);

  // Engine lines with SAN moves and formatted evals for display
  const engineLinesDisplay = useMemo(() => {
    if (engineLines.length === 0) return [];
    return engineLines
      .filter((line) => line?.pv?.length)
      .map((line) => {
        // Convert PV moves from UCI to SAN
        const tempChess = new Chess(displayedFen);
        const sanMoves: string[] = [];
        for (const uci of line!.pv!) {
          try {
            const from = uci.slice(0, 2);
            const to = uci.slice(2, 4);
            const promotion = uci.length > 4 ? uci[4] : undefined;
            const result = tempChess.move({ from, to, promotion });
            if (!result) break;
            sanMoves.push(result.san);
          } catch {
            break;
          }
        }

        // Format eval from side-to-move perspective to white perspective
        let evalText = "";
        if (line!.score) {
          if (line!.score.type === "mate") {
            const mate =
              sideToMove === "w" ? line!.score.value : -line!.score.value;
            evalText = mate >= 0 ? `M${mate}` : `-M${Math.abs(mate)}`;
          } else {
            const cp =
              sideToMove === "w" ? line!.score.value : -line!.score.value;
            const val = cp / 100;
            evalText = `${val >= 0 ? "+" : ""}${val.toFixed(1)}`;
          }
        }

        return {
          sanMoves,
          evalText,
          depth: line!.depth ?? 0,
        };
      });
  }, [engineLines, displayedFen]);

  // Engine-recommended move arrows
  const engineArrows = useMemo(() => {
    if (engineLines.length === 0) return [];
    const seen = new Set<string>();
    const result: { startSquare: string; endSquare: string; color: string }[] =
      [];
    for (let i = 0; i < engineLines.length; i++) {
      const line = engineLines[i];
      if (!line?.pv?.[0]) continue;
      const squares = uciToSquares(line.pv[0]);
      const key = `${squares.startSquare}-${squares.endSquare}`;
      if (seen.has(key)) continue;
      seen.add(key);
      result.push({
        ...squares,
        color:
          ENGINE_ARROW_COLORS[i] ??
          ENGINE_ARROW_COLORS[ENGINE_ARROW_COLORS.length - 1],
      });
    }
    return result;
  }, [engineLines]);

  // Arrows for the current position
  const arrows = useMemo(() => {
    if (isInWhatIf) return [];
    if (effectiveIndex < 0 || !fenBeforeCurrentMove) return [];

    const move = moves[effectiveIndex];
    if (!move) return [];

    const result: { startSquare: string; endSquare: string; color: string }[] =
      [];

    // Only show played/best arrows when there's a different best move
    if (move.best_move_san && move.best_move_san !== move.move_san) {
      const playedSquares = sanToSquares(fenBeforeCurrentMove, move.move_san);
      if (playedSquares) {
        result.push({
          startSquare: playedSquares.from,
          endSquare: playedSquares.to,
          color: "rgba(248, 113, 113, 0.8)",
        });
      }

      const bestSquares = sanToSquares(
        fenBeforeCurrentMove,
        move.best_move_san,
      );
      if (bestSquares) {
        result.push({
          startSquare: bestSquares.from,
          endSquare: bestSquares.to,
          color: "rgba(52, 211, 153, 0.8)",
        });
      }
    }

    return result;
  }, [isInWhatIf, effectiveIndex, fenBeforeCurrentMove, moves]);

  // Merge: played/best arrows take priority over engine arrows on same squares
  const allArrows = useMemo(() => {
    const visible = showEngineArrows ? engineArrows : [];
    const seen = new Set(arrows.map((a) => `${a.startSquare}-${a.endSquare}`));
    const deduped = visible.filter(
      (a) => !seen.has(`${a.startSquare}-${a.endSquare}`),
    );
    const merged = [...deduped, ...arrows];
    return merged.length > 0 ? merged : undefined;
  }, [engineArrows, arrows, showEngineArrows]);

  // Current move data for position info panel
  const currentMove = useMemo(() => {
    if (isInWhatIf || effectiveIndex < 0) return null;
    return moves[effectiveIndex] ?? null;
  }, [isInWhatIf, effectiveIndex, moves]);

  // Live engine eval from top PV line (white perspective)
  const liveEngineEvalCp = useMemo(() => {
    const topLine = engineLines[0];
    if (!topLine?.score) return null;
    const raw = topLine.score.type === "cp" ? topLine.score.value : null;
    if (raw === null) return null;
    return sideToMove === "w" ? raw : -raw;
  }, [engineLines, sideToMove]);

  const liveEngineEvalMate = useMemo(() => {
    const topLine = engineLines[0];
    if (!topLine?.score) return null;
    if (topLine.score.type !== "mate") return null;
    return sideToMove === "w" ? topLine.score.value : -topLine.score.value;
  }, [engineLines, sideToMove]);

  const currentEvalCp = useMemo(() => {
    if (isInWhatIf || effectiveIndex < 0) return null;
    return toWhitePerspective(currentMove?.eval_cp ?? null, effectiveIndex);
  }, [isInWhatIf, effectiveIndex, currentMove]);

  const currentEvalMate = useMemo(() => {
    if (isInWhatIf || effectiveIndex < 0) return null;
    return toWhitePerspectiveMate(
      currentMove?.eval_mate ?? null,
      effectiveIndex,
    );
  }, [isInWhatIf, effectiveIndex, currentMove]);

  const currentBestEvalCp = useMemo(() => {
    if (isInWhatIf || effectiveIndex < 0) return null;
    return toWhitePerspective(
      currentMove?.best_move_eval_cp ?? null,
      effectiveIndex,
    );
  }, [isInWhatIf, effectiveIndex, currentMove]);

  // Eval for the EvalBar during what-if mode (white perspective)
  const whatIfEvalCp = useMemo(() => {
    if (!isInWhatIf || !lastAnalysis || lastAnalysis.moveIndex === null)
      return null;
    return toWhitePerspective(lastAnalysis.playedEval, lastAnalysis.moveIndex);
  }, [isInWhatIf, lastAnalysis]);

  const playedEvalText = useMemo(
    () => formatEvalValue(currentEvalCp, currentEvalMate),
    [currentEvalCp, currentEvalMate],
  );

  const bestEvalText = useMemo(
    () => formatEvalValue(currentBestEvalCp, null),
    [currentBestEvalCp],
  );

  const evalDeltaText = useMemo(
    () => formatEvalDelta(currentMove?.eval_delta ?? null),
    [currentMove],
  );

  // Highlight from/to squares of the last move
  const lastMoveSquares = useMemo((): Record<string, React.CSSProperties> => {
    const style: React.CSSProperties = { background: "rgba(255, 255, 0, 0.4)" };
    if (isInWhatIf && whatIfMoves.length > 0) {
      const last = whatIfMoves[whatIfMoves.length - 1];
      const prevFen =
        whatIfMoves.length > 1
          ? whatIfMoves[whatIfMoves.length - 2].fen
          : effectiveIndex >= 0
            ? (moves[effectiveIndex]?.fen_after ?? startingFen)
            : startingFen;
      const sq = sanToSquares(prevFen, last.san);
      if (!sq) return {};
      return { [sq.from]: style, [sq.to]: style };
    }
    if (effectiveIndex < 0 || !fenBeforeCurrentMove) return {};
    const move = moves[effectiveIndex];
    if (!move) return {};
    const sq = sanToSquares(fenBeforeCurrentMove, move.move_san);
    if (!sq) return {};
    return { [sq.from]: style, [sq.to]: style };
  }, [
    isInWhatIf,
    whatIfMoves,
    effectiveIndex,
    fenBeforeCurrentMove,
    moves,
    startingFen,
  ]);

  // Handle MoveList navigation
  const handleNavigate = useCallback(
    (index: number | null) => {
      if (isInWhatIf) {
        // Clicking a main-line move exits what-if
        setWhatIfMoves([]);
        setWhatIfBranchPoint(-1);
        clearAnalysis();
      }
      setCurrentIndex(index);
    },
    [isInWhatIf, clearAnalysis],
  );

  // Handle piece drop for what-if exploration
  const handleDrop = useCallback(
    ({ sourceSquare, targetSquare }: PieceDropHandlerArgs) => {
      // Determine the FEN to play from
      let baseFen: string;
      if (isInWhatIf) {
        baseFen =
          whatIfMoves.length > 0
            ? whatIfMoves[whatIfMoves.length - 1].fen
            : startingFen;
      } else {
        baseFen = displayedFen;
      }

      try {
        const tempChess = new Chess(baseFen);
        if (!targetSquare) return false;
        const result = tempChess.move({
          from: sourceSquare,
          to: targetSquare,
          promotion: "q",
        });
        if (!result) return false;

        const branchPt = isInWhatIf ? whatIfBranchPoint : effectiveIndex;
        if (!isInWhatIf) {
          // Entering what-if mode
          setWhatIfBranchPoint(effectiveIndex);
        }

        const moveIndex = branchPt + 1 + whatIfMoves.length;
        setWhatIfMoves((prev) => [
          ...prev,
          { san: result.san, fen: tempChess.fen() },
        ]);
        analyzeMove(baseFen, result.san, boardOrientation, moveIndex);
        return true;
      } catch {
        return false;
      }
    },
    [
      isInWhatIf,
      whatIfMoves,
      whatIfBranchPoint,
      startingFen,
      displayedFen,
      effectiveIndex,
      analyzeMove,
      boardOrientation,
    ],
  );

  const handleToggleEngineArrows = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) =>
      setShowEngineArrows(e.target.checked),
    [],
  );

  // Exit what-if mode
  const handleExitWhatIf = useCallback(() => {
    const branchIdx = whatIfBranchPoint;
    setWhatIfMoves([]);
    setWhatIfBranchPoint(-1);
    clearAnalysis();
    setCurrentIndex(branchIdx >= moves.length - 1 ? null : branchIdx);
  }, [whatIfBranchPoint, moves.length, clearAnalysis]);

  return (
    <div className="analysis-board">
      <div className="analysis-board__layout">
        <div className="analysis-board__board-col">
          <div className="analysis-board__board-with-eval">
            <EvalBar
              whitePerspectiveCp={
                isInWhatIf
                  ? ((showEngineArrows ? liveEngineEvalCp : null) ??
                    whatIfEvalCp)
                  : currentEvalCp
              }
              whitePerspectiveMate={
                isInWhatIf
                  ? showEngineArrows
                    ? liveEngineEvalMate
                    : null
                  : currentEvalMate
              }
              whiteOnBottom={boardOrientation === "white"}
            />
            <div className="analysis-board__board-frame">
              <Chessboard
                options={{
                  position: displayedFen,
                  boardOrientation,
                  onPieceDrop: handleDrop,
                  allowDragging: true,
                  animationDurationInMs: 200,
                  squareStyles: lastMoveSquares,
                  arrows: allArrows,
                  boardStyle: {
                    borderRadius: "0",
                    boxShadow: "0 20px 45px rgba(2, 6, 23, 0.5)",
                  },
                }}
              />
            </div>
          </div>
        </div>
        <div className="analysis-board__moves-col">
          <div className="analysis-board__engine-header">
            <label className="analysis-board__toggle">
              <input
                type="checkbox"
                checked={showEngineArrows}
                onChange={handleToggleEngineArrows}
              />
              Engine lines
            </label>
            {showEngineArrows && engineLinesDisplay[0]?.depth > 0 && (
              <span className="analysis-board__engine-depth">
                {engineThinking && (
                  <span className="analysis-board__engine-spinner" />
                )}
                d{engineLinesDisplay[0].depth}
              </span>
            )}
          </div>
          {showEngineArrows && engineLinesDisplay.length > 0 && (
            <div className="analysis-board__engine-lines">
              {[0, 1, 2].map((i) => {
                const line = engineLinesDisplay[i];
                return (
                  <span
                    key={i}
                    className="analysis-board__engine-line"
                    style={{
                      opacity: line ? (i === 0 ? 1 : 0.6) : 0,
                    }}
                  >
                    <span className="analysis-board__engine-eval">
                      {line?.evalText ?? "+0.0"}
                    </span>{" "}
                    <span className="analysis-board__engine-pv">
                      {line?.sanMoves[0] ?? "---"}
                    </span>
                  </span>
                );
              })}
            </div>
          )}
          <MoveList
            moves={moveListMoves}
            currentIndex={moveListIndex}
            onNavigate={handleNavigate}
            playerColor={boardOrientation}
          />
        </div>
      </div>

      {!isInWhatIf && evals.length > 0 && (
        <div className="analysis-board__graph-row">
          <AnalysisGraph
            evals={evals}
            currentIndex={currentIndex}
            onSelectMove={handleNavigate}
          />
          {footer && (
            <div className="analysis-board__graph-footer">{footer}</div>
          )}
        </div>
      )}

      {isInWhatIf && (
        <div className="analysis-board__whatif-bar">
          <span>Exploring alternate line</span>
          <button type="button" onClick={handleExitWhatIf}>
            Exit
          </button>
        </div>
      )}

      {currentMove && !isInWhatIf && (
        <div className="analysis-board__position-info">
          <div className="analysis-board__position-info-row">
            <span className="analysis-board__played-label">
              Played:{" "}
              <strong className="analysis-board__played-move">
                {currentMove.move_san}
              </strong>
              {playedEvalText && (
                <span
                  className={`analysis-board__eval-text ${evalTextClass(currentEvalCp, currentEvalMate)}`}
                >
                  {" "}
                  ({playedEvalText})
                </span>
              )}
            </span>
          </div>
          {currentMove.best_move_san && (
            <div className="analysis-board__position-info-row">
              <span className="analysis-board__best-label">
                Best:{" "}
                <strong className="analysis-board__best-move">
                  {currentMove.best_move_san}
                </strong>
                {bestEvalText && (
                  <span
                    className={`analysis-board__eval-text ${evalTextClass(currentBestEvalCp, null)}`}
                  >
                    {" "}
                    ({bestEvalText})
                  </span>
                )}
              </span>
            </div>
          )}
          <div className="analysis-board__position-info-row">
            {evalDeltaText && (
              <span className="analysis-board__delta-label">
                Delta:{" "}
                <strong className="analysis-board__delta-value">
                  {evalDeltaText}
                </strong>
              </span>
            )}
            {currentMove.classification && (
              <span
                className={`analysis-board__classification analysis-board__classification--${currentMove.classification}`}
              >
                {currentMove.classification}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default memo(AnalysisBoard);
