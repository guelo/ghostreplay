import { useEffect, useRef, useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { ApiError, fetchAnalysis, type SessionAnalysis } from "../utils/api";
import AnalysisBoard from "../components/AnalysisBoard";
import GameReviewStats from "../components/GameReviewStats";
import AppNav from "../components/AppNav";
import { useGameReviewStats } from "../hooks/useGameReviewStats";
import "../App.css";

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;

function GameAnalysisPage() {
  const [searchParams] = useSearchParams();
  const id = searchParams.get("id");

  const [analysis, setAnalysis] = useState<SessionAnalysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [processing, setProcessing] = useState(false);
  const pollCountRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!id) return;

    let cancelled = false;
    pollCountRef.current = 0;
    setProcessing(false);
    setLoading(true);
    setError(null);
    setAnalysis(null);

    const doFetch = (isInitial: boolean) => {
      fetchAnalysis(id)
        .then((data) => {
          if (cancelled) return;
          setAnalysis(data);
          if (isInitial) setLoading(false);

          if (!data.is_complete && pollCountRef.current < POLL_MAX_ATTEMPTS) {
            setProcessing(true);
            pollCountRef.current++;
            pollTimerRef.current = setTimeout(() => {
              if (!cancelled) doFetch(false);
            }, POLL_INTERVAL_MS);
          } else if (data.is_complete) {
            setProcessing(false);
          } else {
            setProcessing(true);
          }
        })
        .catch((err) => {
          if (cancelled) return;

          // Permanent errors (4xx non-retryable) — stop immediately
          const isPermanent = err instanceof ApiError && !err.retryable;
          if (isPermanent) {
            setLoading(false);
            setProcessing(false);
            setError(err.message);
            return;
          }

          // Transient errors — keep polling
          if (isInitial) setLoading(false);
          if (pollCountRef.current < POLL_MAX_ATTEMPTS) {
            setProcessing(true);
            pollCountRef.current++;
            pollTimerRef.current = setTimeout(() => {
              if (!cancelled) doFetch(false);
            }, POLL_INTERVAL_MS);
          } else {
            setProcessing(false);
            setError("Failed to load analysis");
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
  }, [id]);

  const playerColor = analysis?.player_color;
  const missingColor = analysis && !playerColor;

  const { sideStats, highlightedMoves, handleStatHover, handleStatClick, handleGraphMoveClick, pinnedStat, activeStat } =
    useGameReviewStats({
      selectedId: id,
      moves: missingColor ? null : (analysis?.moves ?? null),
      playerColor: playerColor ?? 'white',
    });

  if (!id) {
    return <Navigate to="/play" replace />;
  }

  return (
    <main className="app-shell history-page">
      <AppNav />

      <div className="constrained-content">
        <section className="history-shell">
          {loading && (
            <p className="history-shell__placeholder">Loading analysis...</p>
          )}

          {error && <p className="history-shell__error">{error}</p>}

          {missingColor && (
            <p className="history-shell__error">
              Analysis response missing player color. Please try again later.
            </p>
          )}

          {processing && (
            <p className="analysis-pane__processing">
              Analysis still processing{"\u2026"}
            </p>
          )}

          {!loading && !error && analysis && playerColor && sideStats && (
            <div className="analysis-pane">
              <div className="analysis-pane__shell">
                <AnalysisBoard
                  key={id}
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
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

export default GameAnalysisPage;
