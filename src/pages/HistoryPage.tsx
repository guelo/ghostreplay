import { useEffect, useRef, useState, useCallback } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  fetchHistory,
  fetchAnalysis,
  type HistoryGame,
  type SessionAnalysis,
} from "../utils/api";
import type { OpenHistoryOptions } from "../components/chess-game/types";
import AnalysisBoard, { type AnalysisBoardRef } from "../components/AnalysisBoard";
import GameReviewStats from "../components/GameReviewStats";
import AppNav from "../components/AppNav";
import { useGameReviewStats } from "../hooks/useGameReviewStats";
import "../App.css";

function resultLabel(result: string | null): string {
  switch (result) {
    case "checkmate_win":
      return "Win";
    case "checkmate_loss":
      return "Loss";
    case "resign":
      return "Resigned";
    case "draw":
      return "Draw";
    default:
      return result ?? "Unknown";
  }
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;

function HistoryPage() {
  const location = useLocation();
  const navState = location.state as OpenHistoryOptions | null;

  const [games, setGames] = useState<HistoryGame[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<SessionAnalysis | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisProcessing, setAnalysisProcessing] = useState(false);
  const pollCountRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchHistory()
      .then((data) => {
        if (cancelled) return;
        setGames(data);
        if (data.length > 0) {
          const targetId =
            navState?.sessionId && data.some((g) => g.session_id === navState.sessionId)
              ? navState.sessionId
              : data[0].session_id;
          setAnalysisLoading(true);
          setAnalysis(null);
          setSelectedId(targetId);
        } else {
          setSelectedId(null);
          setAnalysis(null);
          setAnalysisLoading(false);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load history");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selectedId) return;

    let cancelled = false;
    pollCountRef.current = 0;
    setAnalysisProcessing(false);

    const doFetch = (isInitial: boolean) => {
      const fetchPromise = fetchAnalysis(selectedId);
      fetchPromise
        .then((data) => {
          if (cancelled) return;
          setAnalysis(data);
          if (isInitial) setAnalysisLoading(false);

          if (!data.is_complete && pollCountRef.current < POLL_MAX_ATTEMPTS) {
            setAnalysisProcessing(true);
            pollCountRef.current++;
            pollTimerRef.current = setTimeout(() => {
              if (!cancelled) doFetch(false);
            }, POLL_INTERVAL_MS);
          } else if (data.is_complete) {
            setAnalysisProcessing(false);
            fetchHistory()
              .then((fresh) => { if (!cancelled) setGames(fresh); })
              .catch(() => {});
          } else {
            setAnalysisProcessing(true);
          }
        })
        .catch(() => {
          if (cancelled) return;
          if (isInitial) setAnalysisLoading(false);
          if (pollCountRef.current < POLL_MAX_ATTEMPTS) {
            pollCountRef.current++;
            pollTimerRef.current = setTimeout(() => {
              if (!cancelled) doFetch(false);
            }, POLL_INTERVAL_MS);
          }
        });
    };

    doFetch(true);

    return () => {
      cancelled = true;
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [selectedId]);

  const selectedGame = games.find((g) => g.session_id === selectedId) ?? null;
  const playerColor = (selectedGame?.player_color as 'white' | 'black') ?? 'white';

  const boardRef = useRef<AnalysisBoardRef>(null);

  const { sideStats, highlightedMoves, handleStatHover, handleStatClick, handleGraphMoveClick, pinnedStat, activeStat } =
    useGameReviewStats({
      selectedId,
      moves: analysis?.moves ?? null,
      playerColor,
      onJumpToMove: useCallback((index: number) => {
        boardRef.current?.jumpToMove(index);
      }, []),
    });

  return (
    <main className="app-shell history-page">
      <AppNav />

      <div className="constrained-content">
        <section className="history-shell">
          {games.length > 0 && (
            <div className="game-selector-row">
              <select
                className="game-selector"
                value={selectedId ?? ""}
                onChange={(e) => {
                  const id = e.target.value;
                  if (id && id !== selectedId) {
                    setAnalysisLoading(true);
                    setAnalysis(null);
                    setSelectedId(id);
                  }
                }}
              >
                {games.map((g) => (
                  <option key={g.session_id} value={g.session_id}>
                    {resultLabel(g.result)} vs {g.engine_elo} —{" "}
                    {g.ended_at ? formatDate(g.ended_at) : "In progress"} (
                    {g.summary.total_moves} moves)
                  </option>
                ))}
              </select>
              {selectedId && (
                <Link to={`/game?id=${selectedId}`} className="game-share-link" aria-label="Open game analysis link">
                  &#x1F517;
                </Link>
              )}
            </div>
          )}

          {loading && (
            <p className="history-shell__placeholder">Loading games...</p>
          )}

          {error && <p className="history-shell__error">{error}</p>}

          {!loading && !error && games.length === 0 && (
            <div className="history-shell__empty">
              <span className="history-shell__empty-icon" aria-hidden="true">
                {"\u2654"}
              </span>
              <p className="history-shell__empty-title">No games played yet</p>
              <p className="history-shell__placeholder">
                Play your first game to start building your history!
              </p>
              <Link to="/play" className="chess-button primary">
                Start New Game
              </Link>
            </div>
          )}

          {!loading && !error && games.length > 0 && selectedGame && (
            <div className="analysis-pane">
              <div className="analysis-pane__shell">
                {analysisLoading && (
                  <p className="analysis-pane__placeholder">
                    Loading analysis...
                  </p>
                )}

                {analysisProcessing && (
                  <p className="analysis-pane__processing">
                    Analysis still processing{"\u2026"}
                  </p>
                )}

                {!analysisLoading && analysis && sideStats && (
                  <AnalysisBoard
                    ref={boardRef}
                    key={selectedGame.session_id}
                    moves={analysis.moves}
                    boardOrientation={playerColor}
                    initialMoveIndex={analysis.moves.length > 0 ? 0 : undefined}
                    positionAnalysis={analysis.position_analysis}
                    highlightedMoves={highlightedMoves}
                    onGraphMoveClick={handleGraphMoveClick}
                    footer={
                      <GameReviewStats
                        sideStats={sideStats}
                        activeStat={activeStat}
                        pinnedStat={pinnedStat}
                        totalMoves={analysis.moves.length}
                        onStatHover={handleStatHover}
                        onStatClick={handleStatClick}
                      />
                    }
                  />
                )}

                {!analysisLoading && !analysis && (
                  <div className="analysis-pane__summary">
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">{selectedGame.summary.total_moves}</span>
                      <span className="analysis-stat__label">Moves</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">{selectedGame.summary.blunders}</span>
                      <span className="analysis-stat__label">Blunders</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">{selectedGame.summary.mistakes}</span>
                      <span className="analysis-stat__label">Mistakes</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">{selectedGame.summary.inaccuracies}</span>
                      <span className="analysis-stat__label">Inaccuracies</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">{selectedGame.summary.average_centipawn_loss}</span>
                      <span className="analysis-stat__label">Avg CPL</span>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

export default HistoryPage;
