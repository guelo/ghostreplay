import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { ApiError, fetchAnalysis, type SessionAnalysis } from "../utils/api";
import { projectExactBest } from "../utils/projectExactBest";
import AnalysisBoard, { type AnalysisBoardRef } from "../components/AnalysisBoard";
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
  const initialAnalysisRequestRef = useRef<{
    sessionId: string;
    promise: Promise<SessionAnalysis>;
  } | null>(null);

  useEffect(() => {
    if (!id) return;

    let cancelled = false;
    pollCountRef.current = 0;
    // Reset request state when the polled id changes, before kicking off the fetch.
    /* eslint-disable react-hooks/set-state-in-effect */
    setProcessing(false);
    setLoading(true);
    setError(null);
    setAnalysis(null);
    /* eslint-enable react-hooks/set-state-in-effect */

    const getInitialAnalysisRequest = () => {
      const cached = initialAnalysisRequestRef.current;
      if (cached?.sessionId === id) return cached.promise;

      const promise = fetchAnalysis(id);
      initialAnalysisRequestRef.current = { sessionId: id, promise };
      void promise.then(
        () => {
          if (initialAnalysisRequestRef.current?.promise === promise) {
            initialAnalysisRequestRef.current = null;
          }
        },
        () => {
          if (initialAnalysisRequestRef.current?.promise === promise) {
            initialAnalysisRequestRef.current = null;
          }
        },
      );
      return promise;
    };

    const doFetch = (isInitial: boolean) => {
      const request = isInitial ? getInitialAnalysisRequest() : fetchAnalysis(id);
      request
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

  // Mirror the exact-best projection the live/interactive paths already apply
  // (g-kfxj): a played move equal to the TRUSTED position best is promoted to
  // 'best' (loss 0) so its star matches the on-board best-arrow. Project once at
  // this seam and feed the result to every consumer of the moves so the board and
  // the review stats stay internally consistent.
  const projectedMoves = useMemo(
    () =>
      analysis
        ? projectExactBest(analysis.moves, analysis.position_analysis)
        : null,
    [analysis],
  );

  const boardRef = useRef<AnalysisBoardRef>(null);

  const { sideStats, highlightedMoves, handleStatHover, handleStatClick, handleGraphMoveClick, pinnedStat, activeStat } =
    useGameReviewStats({
      selectedId: id,
      moves: missingColor ? null : projectedMoves,
      playerColor: playerColor ?? 'white',
      onJumpToMove: useCallback((index: number) => {
        boardRef.current?.jumpToMove(index);
      }, []),
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

          {!loading && !error && analysis && playerColor && sideStats && projectedMoves && (
            <div className="analysis-pane">
              <div className="analysis-pane__shell">
                <AnalysisBoard
                  key={id}
                  ref={boardRef}
                  moves={projectedMoves}
                  boardOrientation={playerColor}
                  sessionId={id ?? undefined}
                  initialMoveIndex={projectedMoves.length > 0 ? 0 : undefined}
                  positionAnalysis={analysis.position_analysis}
                  highlightedMoves={highlightedMoves}
                  onGraphMoveClick={handleGraphMoveClick}
                  footer={
                    <GameReviewStats
                      sideStats={sideStats}
                      activeStat={activeStat}
                      pinnedStat={pinnedStat}
                      totalMoves={analysis.moves.length}
                      accuracy={analysis.summary.accuracy}
                      accuracyPending={processing}
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
