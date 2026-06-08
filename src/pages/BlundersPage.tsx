import { useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import { Chessboard } from "react-chessboard";
import { Link } from "react-router-dom";
import {
  fetchBlunders,
  fetchAnalysis,
  type BlunderListItem,
  type SessionAnalysis,
} from "../utils/api";
import { normalize_fen } from "../utils/fen";
import AnalysisBoard from "../components/AnalysisBoard";
import AppNav from "../components/AppNav";
import { lookupOpeningByFen, type OpeningLookupResult } from "../openings/openingBook";
import "../App.css";

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const days = Math.floor(diff / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 30) return `${days}d ago`;
  return formatDate(iso);
}

function evalLossDisplay(cp: number): string {
  return `\u2212${(cp / 100).toFixed(1)}`;
}

function isUciMove(notation: string): boolean {
  return /^[a-h][1-8][a-h][1-8][qrbn]?$/i.test(notation);
}

function displayBestMove(fen: string, bestMove: string): string {
  if (!isUciMove(bestMove)) {
    return bestMove;
  }

  try {
    const board = new Chess(fen);
    const move = board.move({
      from: bestMove.slice(0, 2),
      to: bestMove.slice(2, 4),
      promotion: bestMove.slice(4) || undefined,
    });
    return move?.san ?? bestMove;
  } catch {
    return bestMove;
  }
}

function formatOpeningLabel(
  opening: OpeningLookupResult | null | undefined,
  fallbackFamily?: string | null,
): string | null {
  if (opening) {
    return `${opening.eco} ${opening.name}`;
  }
  return fallbackFamily ?? null;
}

const BLUNDER_PAGE_SIZE = 50;
const STARTING_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

/**
 * Determine board orientation from the FEN (whose turn it is = the blunderer's
 * perspective — the side to move at the blunder position).
 */
function orientationFromFen(fen: string): "white" | "black" {
  const parts = fen.split(" ");
  return parts[1] === "w" ? "white" : "black";
}

function fenBeforeMove(analysis: SessionAnalysis, moveIndex: number): string {
  return moveIndex === 0 ? STARTING_FEN : analysis.moves[moveIndex - 1].fen_after;
}

/**
 * Find the move index in the analysis that corresponds to the blunder position.
 * The blunder FEN is the position BEFORE the bad move, so we look for a move
 * whose preceding fen_after matches (or index 0 if the blunder is at the start).
 */
function findBlunderMoveIndex(
  analysis: SessionAnalysis,
  blunderFen: string,
  badMoveSan: string,
): number | undefined {
  const targetNorm = normalize_fen(blunderFen);

  for (let i = 0; i < analysis.moves.length; i++) {
    const move = analysis.moves[i];
    const fenBefore = fenBeforeMove(analysis, i);

    if (normalize_fen(fenBefore) === targetNorm && move.move_san === badMoveSan) {
      return i;
    }
  }

  return undefined;
}

async function deriveOpeningFromAnalysis(
  analysis: SessionAnalysis,
  blunder: BlunderListItem,
): Promise<OpeningLookupResult | null> {
  const targetNorm = normalize_fen(blunder.fen);
  let lastKnown: OpeningLookupResult | null = null;

  for (let i = 0; i < analysis.moves.length; i++) {
    const move = analysis.moves[i];
    const fenBefore = fenBeforeMove(analysis, i);

    try {
      const opening = await lookupOpeningByFen(fenBefore);
      if (opening) {
        lastKnown = opening;
      }
    } catch {
      // Missing opening assets should not block the blunder library.
    }

    if (normalize_fen(fenBefore) === targetNorm && move.move_san === blunder.bad_move) {
      return lastKnown;
    }
  }

  return lastKnown;
}

function BlundersPage() {
  const [blunders, setBlunders] = useState<BlunderListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [practiceReadyTotal, setPracticeReadyTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [readyOnly, setReadyOnly] = useState(false);
  const [openingByBlunderId, setOpeningByBlunderId] = useState<
    Record<number, OpeningLookupResult | null>
  >({});

  const [analysis, setAnalysis] = useState<SessionAnalysis | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const requestGenerationRef = useRef(0);
  const openingAnalysisLookupRef = useRef<Set<number>>(new Set());

  const handleToggleReadyOnly = () => {
    setReadyOnly((v) => !v);
    setSelectedId(null);
    setAnalysis(null);
    setAnalysisLoading(false);
  };

  useEffect(() => {
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    setLoading(true);
    setLoadingMore(false);
    setError(null);
    setSelectedId(null);
    setAnalysis(null);
    setAnalysisLoading(false);

    fetchBlunders({ practiceReady: readyOnly, limit: BLUNDER_PAGE_SIZE, offset: 0 })
      .then((data) => {
        if (requestGenerationRef.current !== generation) return;
        setBlunders(data.items);
        setTotal(data.total);
        setPracticeReadyTotal(data.practice_ready_total);
      })
      .catch((err) => {
        if (requestGenerationRef.current !== generation) return;
        setError(err instanceof Error ? err.message : "Failed to load blunders");
      })
      .finally(() => {
        if (requestGenerationRef.current === generation) setLoading(false);
      });
  }, [readyOnly]);

  const selected = blunders.find((b) => b.id === selectedId) ?? null;
  const selectedAnalysisSessionId =
    selected?.source_session_id ?? selected?.last_session_id ?? null;
  const selectedOpeningLabel = selected
    ? formatOpeningLabel(openingByBlunderId[selected.id], selected.opening_family)
    : null;

  const handleLoadMore = () => {
    if (loadingMore || blunders.length >= total) return;
    const generation = requestGenerationRef.current;
    const offset = blunders.length;
    setLoadingMore(true);
    setError(null);

    fetchBlunders({ practiceReady: readyOnly, limit: BLUNDER_PAGE_SIZE, offset })
      .then((data) => {
        if (requestGenerationRef.current !== generation) return;
        setBlunders((current) => [...current, ...data.items]);
        setTotal(data.total);
        setPracticeReadyTotal(data.practice_ready_total);
      })
      .catch((err) => {
        if (requestGenerationRef.current !== generation) return;
        setError(err instanceof Error ? err.message : "Failed to load blunders");
      })
      .finally(() => {
        if (requestGenerationRef.current === generation) setLoadingMore(false);
      });
  };

  // Fetch full game analysis when a blunder with a session is selected
  useEffect(() => {
    setAnalysis(null);
    setAnalysisLoading(!!selectedAnalysisSessionId);
    if (!selectedAnalysisSessionId) {
      return;
    }

    let cancelled = false;
    fetchAnalysis(selectedAnalysisSessionId)
      .then((data) => {
        if (!cancelled) setAnalysis(data);
      })
      .catch(() => {
        // Analysis not available — detail pane will show position-only view
      })
      .finally(() => {
        if (!cancelled) setAnalysisLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected?.id, selectedAnalysisSessionId]);

  useEffect(() => {
    let cancelled = false;
    const blundersToLookup = blunders.filter(
      (blunder) => openingByBlunderId[blunder.id] === undefined,
    );
    if (blundersToLookup.length === 0) {
      return;
    }

    Promise.all(
      blundersToLookup.map(async (blunder) => {
        try {
          return [blunder.id, await lookupOpeningByFen(blunder.fen)] as const;
        } catch {
          return [blunder.id, null] as const;
        }
      }),
    ).then((entries) => {
      if (cancelled) return;
      setOpeningByBlunderId((current) => {
        const next = { ...current };
        for (const [id, opening] of entries) {
          next[id] = opening;
        }
        return next;
      });
    });

    return () => {
      cancelled = true;
    };
  }, [blunders, openingByBlunderId]);

  useEffect(() => {
    if (!selected || !analysis || openingByBlunderId[selected.id]) {
      return;
    }

    let cancelled = false;
    deriveOpeningFromAnalysis(analysis, selected).then((opening) => {
      if (cancelled || !opening) return;
      setOpeningByBlunderId((current) => ({
        ...current,
        [selected.id]: opening,
      }));
    });

    return () => {
      cancelled = true;
    };
  }, [analysis, selected, openingByBlunderId]);

  useEffect(() => {
    const candidates = blunders.filter(
      (blunder) =>
        openingByBlunderId[blunder.id] === null &&
        !blunder.opening_family &&
        !!blunder.source_session_id &&
        !openingAnalysisLookupRef.current.has(blunder.id),
    );
    if (candidates.length === 0) {
      return;
    }

    let cancelled = false;
    for (const blunder of candidates) {
      openingAnalysisLookupRef.current.add(blunder.id);
    }

    const bySession = new Map<string, BlunderListItem[]>();
    for (const blunder of candidates) {
      const sessionId = blunder.source_session_id;
      if (!sessionId) continue;
      bySession.set(sessionId, [...(bySession.get(sessionId) ?? []), blunder]);
    }

    Promise.all(
      Array.from(bySession.entries()).map(async ([sessionId, sessionBlunders]) => {
        try {
          const sourceAnalysis = await fetchAnalysis(sessionId);
          const entries = await Promise.all(
            sessionBlunders.map(async (blunder) => [
              blunder.id,
              await deriveOpeningFromAnalysis(sourceAnalysis, blunder),
            ] as const),
          );
          return entries;
        } catch {
          return [] as (readonly [number, OpeningLookupResult | null])[];
        }
      }),
    ).then((groups) => {
      if (cancelled) return;
      const entries = groups.flat().filter((entry): entry is readonly [number, OpeningLookupResult] => entry[1] !== null);
      if (entries.length === 0) return;
      setOpeningByBlunderId((current) => {
        const next = { ...current };
        for (const [id, opening] of entries) {
          next[id] = opening;
        }
        return next;
      });
    });

    return () => {
      cancelled = true;
      for (const blunder of candidates) {
        openingAnalysisLookupRef.current.delete(blunder.id);
      }
    };
  }, [blunders, openingByBlunderId]);

  const boardOrientation = useMemo(
    () => (selected ? orientationFromFen(selected.fen) : "white"),
    [selected],
  );

  const blunderMoveIndex = useMemo(() => {
    if (!analysis || !selected) return undefined;
    return findBlunderMoveIndex(analysis, selected.fen, selected.bad_move);
  }, [analysis, selected]);

  const hasMore = blunders.length < total;

  return (
    <main className="app-shell">
      <AppNav />

      <div className="constrained-content">
        <section className="blunders-shell">
          <div className="blunders-shell__header">
            <h1 className="blunders-shell__title">Blunder Library</h1>
            <div className="blunders-shell__summary">
              <span className="blunders-shell__count">
                {readyOnly ? `${blunders.length} of ${total} ready` : `${total} total`}
              </span>
              {!readyOnly && practiceReadyTotal !== null && (
                <span className="blunders-shell__count blunders-shell__count--due">
                  {practiceReadyTotal} ready
                </span>
              )}
              <button
                type="button"
                className={`chess-button toggle${readyOnly ? " active" : ""}`}
                onClick={handleToggleReadyOnly}
              >
                {readyOnly ? "Show all" : "Practice-ready"}
              </button>
            </div>
          </div>

          {loading && (
            <p className="blunders-shell__placeholder">Loading blunders...</p>
          )}

          {error && <p className="blunders-shell__error">{error}</p>}

          {!loading && !error && blunders.length === 0 && (
            <div className="blunders-shell__empty">
              <span className="blunders-shell__empty-icon" aria-hidden="true">
                {"\u2654"}
              </span>
              <p className="blunders-shell__empty-title">
                {readyOnly ? "No practice-ready blunders" : "No blunders recorded yet"}
              </p>
              <p className="blunders-shell__placeholder">
                {readyOnly
                  ? "Nothing the ghost can steer to right now. Play more games to keep learning."
                  : "Play games and your blunders will appear here for review."}
              </p>
              <Link to="/play" className="chess-button primary">
                Start New Game
              </Link>
            </div>
          )}

          {!loading && !error && blunders.length > 0 && (
            <div className="blunders-layout">
              <ul className="blunder-list" role="listbox" aria-label="Blunder library">
                {blunders.map((b) => (
                  <li key={b.id}>
                    <button
                      className={`blunder-card${b.id === selectedId ? " blunder-card--selected" : ""}`}
                      type="button"
                      role="option"
                      aria-selected={b.id === selectedId}
                      onClick={() => setSelectedId(b.id)}
                    >
                      <div className="blunder-card__info">
                        <div className="blunder-card__moves">
                          <span className="blunder-card__bad">
                            {b.bad_move}
                          </span>
                          <span className="blunder-card__arrow">{"\u2192"}</span>
                          <span className="blunder-card__best">
                            {displayBestMove(b.fen, b.best_move)}
                          </span>
                        </div>
                        <div className="blunder-card__meta">
                          <span className="blunder-card__eval">
                            {evalLossDisplay(b.eval_loss_cp)}
                          </span>
                          <span
                            className={`blunder-card__due ${
                              b.ghost_eligible
                                ? "blunder-card__due--urgent"
                                : "blunder-card__due--ok"
                            }`}
                          >
                            {b.ghost_eligible ? "Ready" : "Off-radar"}
                          </span>
                        </div>
                        {formatOpeningLabel(openingByBlunderId[b.id], b.opening_family) && (
                          <span className="blunder-card__opening">
                            {formatOpeningLabel(openingByBlunderId[b.id], b.opening_family)}
                          </span>
                        )}
                        {b.last_played_at && (
                          <span className="blunder-card__date">
                            Last Played: {formatRelative(b.last_played_at)}
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                ))}
                {hasMore && (
                  <li className="blunder-list__load-more">
                    <button
                      type="button"
                      className="chess-button"
                      onClick={handleLoadMore}
                      disabled={loadingMore}
                    >
                      {loadingMore ? "Loading..." : "Load more"}
                    </button>
                  </li>
                )}
              </ul>

              <div className="blunder-detail">
                {selected ? (
                  <div className="blunder-detail__shell">
                    <div className="blunder-detail__board-area">
                      {analysisLoading && (
                        <p className="blunder-detail__placeholder">
                          Loading game analysis...
                        </p>
                      )}

                      {!analysisLoading && analysis && analysis.moves.length > 0 ? (
                        <AnalysisBoard
                          key={selected.id}
                          moves={analysis.moves}
                          boardOrientation={boardOrientation}
                          initialMoveIndex={blunderMoveIndex}
                          positionAnalysis={analysis.position_analysis}
                        />
                      ) : !analysisLoading ? (
                        <Chessboard
                          options={{
                            position: selected.fen,
                            boardOrientation,
                            allowDragging: false,
                          }}
                        />
                      ) : null}
                    </div>

                    <div className="blunder-detail__metadata">
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Opening</span>
                        <span className="blunder-detail__stat-value blunder-detail__stat-value--compact">
                          {selectedOpeningLabel ?? "Unknown"}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Eval loss</span>
                        <span className="blunder-detail__stat-value blunder-detail__stat-value--loss">
                          {evalLossDisplay(selected.eval_loss_cp)}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Practice priority</span>
                        <span className="blunder-detail__stat-value">
                          {selected.practice_priority_score.toFixed(2)}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">SRS due</span>
                        <span className="blunder-detail__stat-value">
                          {selected.srs_due ? "Yes" : "No"}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Pass streak</span>
                        <span className="blunder-detail__stat-value">
                          {selected.pass_streak}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Pass / Fail</span>
                        <span className="blunder-detail__stat-value">
                          {(selected.review_count ?? 0) > 0
                            ? `${selected.pass_count ?? 0}/${selected.fail_count ?? 0}`
                            : "—"}
                        </span>
                        <span className="blunder-detail__stat-subtext">
                          {selected.review_count ?? 0} reviews
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Recent</span>
                        <span
                          className={`blunder-detail__result-chip ${
                            (selected.last_result ?? null) === true
                              ? "blunder-detail__result-chip--pass"
                              : (selected.last_result ?? null) === false
                                ? "blunder-detail__result-chip--fail"
                                : "blunder-detail__result-chip--neutral"
                          }`}
                        >
                          {(selected.last_result ?? null) === true
                            ? "Pass"
                            : (selected.last_result ?? null) === false
                              ? "Fail"
                              : "Not reviewed"}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Last reviewed</span>
                        <span className="blunder-detail__stat-value">
                          {selected.last_reviewed_at
                            ? formatRelative(selected.last_reviewed_at)
                            : "Never"}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Last played</span>
                        <span className="blunder-detail__stat-value">
                          {selected.last_played_at
                            ? formatRelative(selected.last_played_at)
                            : "Unknown"}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Recorded</span>
                        <span className="blunder-detail__stat-value">
                          {formatDate(selected.created_at)}
                        </span>
                      </div>
                    </div>
                  </div>
                ) : (
                  <p className="blunder-detail__placeholder">
                    Select a blunder to study.
                  </p>
                )}
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

export default BlundersPage;
