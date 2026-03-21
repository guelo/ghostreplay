import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";
import type { PieceDropHandlerArgs } from "react-chessboard";
import type { AnalysisMove, PositionAnalysis } from "../utils/api";
import type { EngineInfo } from "../workers/stockfishMessages";
import { useMoveAnalysis } from "../hooks/useMoveAnalysis";
import { useStockfishEngine } from "../hooks/useStockfishEngine";
import { createAnalysisStore } from "../stores/createAnalysisStore";
import { useStore } from "zustand";
import { mateToCp, playerToWhite, toWhitePerspective } from "../workers/analysisUtils";
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
  positionAnalysis?: Record<string, PositionAnalysis>;
};

type WhatIfMove = {
  san: string;
  fen: string;
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

const BEST_MOVE_ARROW_COLOR = "rgba(59, 130, 246, 0.85)";

/** Grey arrow whose opacity fades as centipawn loss grows. */
export const engineArrowColor = (cpLoss: number): string => {
  const clamped = Math.max(0, cpLoss);
  const opacity = Math.max(0.2, Math.min(0.7, 0.7 - clamped / 300));
  return `rgba(150, 150, 150, ${opacity.toFixed(2)})`;
};

const DEFAULT_GREY_ARROW = "rgba(150, 150, 150, 0.45)";

type MoveArrow = { startSquare: string; endSquare: string; color: string };

/** Convert an EngineScore to a single number (side-to-move relative). */
const scoreToNum = (s: EngineInfo["score"]): number | null => {
  if (!s) return null;
  return s.type === "cp" ? s.value : mateToCp(s.value);
};

/** Pure function: build engine line arrows with strength-based styling. */
export function buildEngineArrows(
  lines: EngineInfo[],
): MoveArrow[] {
  if (lines.length === 0) return [];
  const scores = lines.map((l) => scoreToNum(l?.score));
  const bestScore = scores.find((s) => s !== null) ?? null;

  const seen = new Set<string>();
  const result: MoveArrow[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line?.pv?.[0]) continue;
    const squares = uciToSquares(line.pv[0]);
    const key = `${squares.startSquare}-${squares.endSquare}`;
    if (seen.has(key)) continue;
    seen.add(key);

    let color: string;
    if (i === 0) {
      color = BEST_MOVE_ARROW_COLOR;
    } else if (bestScore !== null && scores[i] !== null) {
      color = engineArrowColor(bestScore - scores[i]!);
    } else {
      color = DEFAULT_GREY_ARROW;
    }

    result.push({ ...squares, color });
  }
  return result;
}

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
  positionAnalysis,
}: AnalysisBoardProps) => {
  const [currentIndex, setCurrentIndex] = useState<number | null>(
    initialMoveIndex ?? null,
  );
  const [whatIfMoves, setWhatIfMoves] = useState<WhatIfMove[]>([]);
  const [whatIfBranchPoint, setWhatIfBranchPoint] = useState(-1);
  const [analysisStore] = useState(() => createAnalysisStore());
  const { analyzeMove, clearAnalysis } = useMoveAnalysis(analysisStore);
  const analysisMap = useStore(analysisStore, (s) => s.analysisMap);
  const lastAnalysis = useStore(analysisStore, (s) => s.lastAnalysis);
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
        classification: m.classification,
        eval: toWhitePerspective(m.eval_cp, i),
      })),
    [moves],
  );

  // Extract eval values for the graph, falling back to mateToCp for mate-only moves
  const evals = useMemo(
    () =>
      moves.map((m, i) => {
        // eval_cp is mover-perspective. mateToCp gives side-to-move (position)
        // perspective, so negate it to get mover-perspective before converting.
        const cp = m.eval_cp ?? (m.eval_mate != null ? -mateToCp(m.eval_mate) : null);
        return toWhitePerspective(cp, i);
      }),
    [moves],
  );

  // Combined moves for MoveList when in what-if mode
  const moveListMoves = useMemo(() => {
    if (!isInWhatIf) return mappedMoves;
    const base = mappedMoves.slice(0, whatIfBranchPoint + 1);
    const branch = whatIfMoves.map((m, i) => {
      const absIndex = whatIfBranchPoint + 1 + i;
      const analysis = analysisMap.get(absIndex);
      return {
        san: m.san,
        classification: analysis?.classification ?? undefined,
        // playedEval is player-perspective; convert to white perspective
        eval:
          analysis?.playedEval != null
            ? playerToWhite(analysis.playedEval, boardOrientation) ?? undefined
            : undefined,
      };
    });
    return [...base, ...branch];
  }, [isInWhatIf, mappedMoves, whatIfBranchPoint, whatIfMoves, analysisMap, boardOrientation]);

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

  // Stop engine and clear lines synchronously when position changes.
  // engineStaleRef prevents using leftover engine data with the new
  // sideToMove for one frame (setInfo is async, but the ref is instant).
  const prevFenRef = useRef(displayedFen);
  const engineStaleRef = useRef(false);
  if (prevFenRef.current !== displayedFen) {
    prevFenRef.current = displayedFen;
    engineStaleRef.current = true;
    stopSearch();
  }

  // Side-to-move derived from FEN (avoids constructing Chess just for turn())
  const sideToMove = useMemo(() => fenSideToMove(displayedFen), [displayedFen]);

  // Cached best move for the displayed position (from pre-existing game analysis)
  const cachedBest = positionAnalysis?.[displayedFen] ?? null;

  // Legal moves excluding cached best move (for restricted engine search)
  const searchmoves = useMemo(() => {
    if (!cachedBest) return undefined;
    try {
      const chess = new Chess(displayedFen);
      const allMoves = chess.moves({ verbose: true });
      const filtered = allMoves
        .map((m) => m.from + m.to + (m.promotion ?? ""))
        .filter((uci) => uci !== cachedBest.best_move_uci);
      return filtered.length > 0 ? filtered : undefined;
    } catch {
      return undefined;
    }
  }, [displayedFen, cachedBest]);

  // Start new evaluation after render
  useEffect(() => {
    if (!displayedFen || !showEngineArrows) return;
    engineStaleRef.current = false;
    if (cachedBest && searchmoves && searchmoves.length > 0) {
      evaluatePosition(displayedFen, { depth: 21, multipv: 2, searchmoves }).catch(() => {});
    } else {
      evaluatePosition(displayedFen, { depth: 21, multipv: 3 }).catch(() => {});
    }
  }, [displayedFen, evaluatePosition, showEngineArrows, cachedBest, searchmoves]);

  // Whether the restricted search path is active (same condition as the engine request)
  const useRestrictedSearch = !!(cachedBest && searchmoves && searchmoves.length > 0);

  // Merge cached best move into engine lines so arrows and panel stay in sync.
  // Only merge when the restricted search was actually used — otherwise the
  // engine already searched for the full top-line set including the best move.
  // EngineInfo.score.value is side-to-move-relative; best_move_eval_cp is also
  // side-to-move-relative, so we pass it through without sign conversion.
  const mergedEngineLines: EngineInfo[] = useMemo(() => {
    if (!useRestrictedSearch || !cachedBest) return engineLines;

    const cachedLine: EngineInfo = {
      pv: [cachedBest.best_move_uci],
      score:
        cachedBest.best_move_eval_cp != null
          ? { type: "cp" as const, value: cachedBest.best_move_eval_cp }
          : undefined,
      depth: undefined,
    };

    return [cachedLine, ...engineLines];
  }, [useRestrictedSearch, cachedBest, engineLines]);

  // Engine lines with SAN moves and formatted evals for display
  const engineLinesDisplay = useMemo(() => {
    if (mergedEngineLines.length === 0) return [];
    return mergedEngineLines
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
  }, [mergedEngineLines, displayedFen]);

  // Engine-recommended move arrows — best move is blue, others grey with
  // opacity proportional to their centipawn loss relative to the best move.
  const engineArrows = useMemo(
    () => buildEngineArrows(mergedEngineLines),
    [mergedEngineLines],
  );

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
    if (engineStaleRef.current) return null;
    const topLine = mergedEngineLines[0];
    if (!topLine?.score) return null;
    const raw = topLine.score.type === "cp" ? topLine.score.value : null;
    if (raw === null) return null;
    return sideToMove === "w" ? raw : -raw;
  }, [mergedEngineLines, sideToMove]);

  const liveEngineEvalMate = useMemo(() => {
    if (engineStaleRef.current) return null;
    const topLine = mergedEngineLines[0];
    if (!topLine?.score) return null;
    if (topLine.score.type !== "mate") return null;
    return sideToMove === "w" ? topLine.score.value : -topLine.score.value;
  }, [mergedEngineLines, sideToMove]);

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
    // playedEval is player-perspective; convert to white perspective
    return playerToWhite(lastAnalysis.playedEval, boardOrientation);
  }, [isInWhatIf, lastAnalysis, boardOrientation]);

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
        const uciMove = `${sourceSquare}${targetSquare}${result.promotion ?? ""}`;
        analyzeMove(baseFen, uciMove, boardOrientation, moveIndex);
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
            {showEngineArrows && engineLines[0]?.depth != null && engineLines[0].depth > 0 && (
              <span className="analysis-board__engine-depth">
                {engineThinking && (
                  <span className="analysis-board__engine-spinner" />
                )}
                d{engineLines[0].depth}
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
            playerColor={boardOrientation}
            evalCp={currentEvalCp ?? evals[effectiveIndex] ?? null}
            isCheckmate={currentEvalMate === 0}
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
