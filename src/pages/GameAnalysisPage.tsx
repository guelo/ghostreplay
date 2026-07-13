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
  // Terminal outcome of polling: the payload never completed ('incomplete'), or a poll
  // failed for good after a payload was already in hand ('stale'). Null while polling.
  const [pollOutcome, setPollOutcome] = useState<null | 'incomplete' | 'stale'>(null);
  const hasPayloadRef = useRef(false);
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
    hasPayloadRef.current = false;
    // Reset request state when the polled id changes, before kicking off the fetch.
    /* eslint-disable react-hooks/set-state-in-effect */
    setPollOutcome(null);
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
          hasPayloadRef.current = true;
          setAnalysis(data);
          // Unconditional: a retry runs with isInitial === false, and under an
          // isInitial guard a successful retry would never clear `loading`.
          setLoading(false);

          if (!data.is_complete && pollCountRef.current < POLL_MAX_ATTEMPTS) {
            setProcessing(true);
            pollCountRef.current++;
            pollTimerRef.current = setTimeout(() => {
              if (!cancelled) doFetch(false);
            }, POLL_INTERVAL_MS);
          } else if (data.is_complete) {
            setProcessing(false);
          } else {
            // Out of attempts on a payload that never completed. Nothing server-side
            // will finish it, so stop claiming it is still processing.
            setProcessing(false);
            setPollOutcome('incomplete');
          }
        })
        .catch((err) => {
          if (cancelled) return;

          const isPermanent = err instanceof ApiError && !err.retryable;
          const retriesLeft = pollCountRef.current < POLL_MAX_ATTEMPTS;

          if (!isPermanent && retriesLeft) {
            // Retry window. `processing` describes the PAYLOAD, not the request, so it
            // is never set here. With no payload we simply stay in `loading`; with one,
            // a failed refresh is silent until the retries run out.
            pollCountRef.current++;
            pollTimerRef.current = setTimeout(() => {
              if (!cancelled) doFetch(false);
            }, POLL_INTERVAL_MS);
            return;
          }

          // Terminal: a permanent ApiError, or transient retries exhausted.
          setProcessing(false);
          if (hasPayloadRef.current) {
            // Keep the board up — setting `error` here is what would blank it.
            setPollOutcome('stale');
            return;
          }
          setLoading(false);
          setError(isPermanent ? err.message : "Failed to load analysis");
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

          {!loading && !error && analysis && playerColor && sideStats && projectedMoves && (
            <div className="analysis-pane">
              <div className="analysis-pane__shell">
                {processing && (
                  <p className="analysis-pane__processing">
                    Analysis still processing{"\u2026"}
                  </p>
                )}

                {!processing && pollOutcome === 'incomplete' && (
                  <p className="analysis-pane__notice">
                    Analysis incomplete {"\u2014"} some moves were never evaluated
                  </p>
                )}

                {!processing && pollOutcome === 'stale' && (
                  <p className="analysis-pane__notice">
                    Couldn{"\u2019"}t refresh {"\u2014"} showing the last loaded result
                  </p>
                )}

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
