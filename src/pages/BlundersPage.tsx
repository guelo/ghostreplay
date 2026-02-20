import { useEffect, useMemo, useState } from "react";
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

/**
 * Determine board orientation from the FEN (whose turn it is = the blunderer's
 * perspective — the side to move at the blunder position).
 */
function orientationFromFen(fen: string): "white" | "black" {
  const parts = fen.split(" ");
  return parts[1] === "w" ? "white" : "black";
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
    // The FEN before this move is the previous move's fen_after, or starting position
    const fenBefore =
      i === 0
        ? "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        : analysis.moves[i - 1].fen_after;

    if (normalize_fen(fenBefore) === targetNorm && move.move_san === badMoveSan) {
      return i;
    }
  }

  return undefined;
}

function BlundersPage() {
  const [blunders, setBlunders] = useState<BlunderListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [dueOnly, setDueOnly] = useState(false);

  const [analysis, setAnalysis] = useState<SessionAnalysis | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchBlunders(dueOnly)
      .then((data) => {
        if (cancelled) return;
        setBlunders(data);
        if (data.length > 0) {
          setSelectedId((prev) => {
            if (prev !== null && data.some((b) => b.id === prev)) return prev;
            return data[0].id;
          });
        } else {
          setSelectedId(null);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load blunders");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [dueOnly]);

  const selected = blunders.find((b) => b.id === selectedId) ?? null;

  // Fetch full game analysis when a blunder with a session is selected
  useEffect(() => {
    if (!selected?.last_session_id) {
      setAnalysis(null);
      return;
    }

    let cancelled = false;
    setAnalysisLoading(true);
    setAnalysis(null);
    fetchAnalysis(selected.last_session_id)
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
  }, [selected?.id, selected?.last_session_id]);

  const dueCount = useMemo(
    () => blunders.filter((b) => b.srs_priority > 1.0).length,
    [blunders],
  );

  const boardOrientation = useMemo(
    () => (selected ? orientationFromFen(selected.fen) : "white"),
    [selected],
  );

  const blunderMoveIndex = useMemo(() => {
    if (!analysis || !selected) return undefined;
    return findBlunderMoveIndex(analysis, selected.fen, selected.bad_move);
  }, [analysis, selected]);

  return (
    <main className="app-shell">
      <AppNav />

      <div className="constrained-content">
        <section className="blunders-shell">
          <div className="blunders-shell__header">
            <h1 className="blunders-shell__title">Blunder Library</h1>
            <div className="blunders-shell__summary">
              <span className="blunders-shell__count blunders-shell__count--due">
                {dueOnly ? blunders.length : dueCount} due
              </span>
              <span className="blunders-shell__count">
                {dueOnly ? `of ${blunders.length} shown` : `${blunders.length} total`}
              </span>
              <button
                type="button"
                className={`chess-button toggle${dueOnly ? " active" : ""}`}
                onClick={() => setDueOnly((v) => !v)}
              >
                {dueOnly ? "Show all" : "Due only"}
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
                {dueOnly ? "No blunders due for review" : "No blunders recorded yet"}
              </p>
              <p className="blunders-shell__placeholder">
                {dueOnly
                  ? "All caught up! Play more games to keep learning."
                  : "Play games and your blunders will appear here for review."}
              </p>
              <Link to="/game" className="chess-button primary">
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
                      <div className="blunder-card__board">
                        <Chessboard
                          options={{
                            position: b.fen,
                            boardOrientation: orientationFromFen(b.fen),
                            allowDragging: false,
                            boardStyle: {
                              borderRadius: "4px",
                              pointerEvents: "none",
                            },
                          }}
                        />
                      </div>
                      <div className="blunder-card__info">
                        <div className="blunder-card__moves">
                          <span className="blunder-card__bad">
                            {b.bad_move}
                          </span>
                          <span className="blunder-card__arrow">{"\u2192"}</span>
                          <span className="blunder-card__best">
                            {b.best_move}
                          </span>
                        </div>
                        <div className="blunder-card__meta">
                          <span className="blunder-card__eval">
                            {evalLossDisplay(b.eval_loss_cp)}
                          </span>
                          <span
                            className={`blunder-card__due ${
                              b.srs_priority > 1.0
                                ? "blunder-card__due--urgent"
                                : "blunder-card__due--ok"
                            }`}
                          >
                            {b.srs_priority > 1.0 ? "Due" : "Not due"}
                          </span>
                        </div>
                        {b.last_played_at && (
                          <span className="blunder-card__date">
                            {formatRelative(b.last_played_at)}
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                ))}
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
                        <span className="blunder-detail__stat-label">Eval loss</span>
                        <span className="blunder-detail__stat-value blunder-detail__stat-value--loss">
                          {evalLossDisplay(selected.eval_loss_cp)}
                        </span>
                      </div>
                      <div className="blunder-detail__stat">
                        <span className="blunder-detail__stat-label">Pass streak</span>
                        <span className="blunder-detail__stat-value">
                          {selected.pass_streak}
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
