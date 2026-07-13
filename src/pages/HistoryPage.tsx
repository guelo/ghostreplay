import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  fetchHistory,
  fetchAnalysis,
  type HistoryGame,
  type SessionAnalysis,
  type OpeningLineageItem,
} from "../utils/api";
import type { OpenHistoryOptions } from "../components/chess-game/types";
import AnalysisBoard, { type AnalysisBoardRef } from "../components/AnalysisBoard";
import GameSelector from "../components/GameSelector";
import GameReviewStats from "../components/GameReviewStats";
import GameOpeningLineage from "../components/GameOpeningLineage";
import AppNav from "../components/AppNav";
import { useGameReviewStats } from "../hooks/useGameReviewStats";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { useSessionOpenings } from "../hooks/useSessionOpenings";
import { projectExactBest } from "../utils/projectExactBest";
import { captureEvent } from "../analytics/posthog";
import "../App.css";

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;

function HistoryPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const navState = location.state as OpenHistoryOptions | null;
  const isNarrow = useMediaQuery("(max-width: 720px)");

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

  // Opening lineage once analysis exists / when its move set grows. Keyed off
  // analysis.moves.length (not selectedId alone) so a one-shot fetch does not
  // return [] while session moves are still arriving. A null sessionId (no moves
  // yet) disables the hook entirely, preserving the zero-move fetch skip. The
  // game is finished here, so no polling.
  const analysisMoveCount = analysis?.moves.length ?? 0;
  const { lineage: openingLineage, startPly: openingStartPly } =
    useSessionOpenings(analysisMoveCount > 0 ? selectedId : null, {
      refetchKey: analysisMoveCount,
    });

  const selectedGame = games.find((g) => g.session_id === selectedId) ?? null;
  const playerColor = (selectedGame?.player_color as 'white' | 'black') ?? 'white';
  const handleGameChange = useCallback((id: string) => {
    if (id && id !== selectedId) {
      captureEvent("history_game_selected", {
        session_id: id,
        result: games.find((g) => g.session_id === id)?.result ?? null,
      });
      setAnalysisLoading(true);
      setAnalysis(null);
      setSelectedId(id);
    }
  }, [selectedId, games]);

  const gameSelector = games.length > 0 ? (
    <div className="game-selector-row">
      <GameSelector
        games={games}
        selectedId={selectedId}
        onChange={handleGameChange}
      />
      {selectedId && (
        <Link to={`/game?id=${selectedId}`} className="game-share-link" aria-label="Open game analysis link">
          &#x1F517;
        </Link>
      )}
    </div>
  ) : null;

  const boardRef = useRef<AnalysisBoardRef>(null);

  const handleSelectRoot = useCallback(
    (item: OpeningLineageItem) => {
      // `item.moves` is the played SAN prefix up to and INCLUDING the crossing
      // move, so its last index is that move's index in `analysis.moves` (same
      // session-move ordering) — a per-crossing index, so a repeated opening root
      // jumps to ITS crossing rather than a first FEN match.
      const idx = item.moves.length - 1;
      const moveCount = analysis?.moves.length ?? 0;
      if (idx >= 0 && idx < moveCount) {
        boardRef.current?.jumpToMove(idx);
      }
    },
    [analysis],
  );

  const handleStartDrill = useCallback(
    (item: OpeningLineageItem) => {
      navigate("/play", {
        state: {
          drillSetup: {
            openingKey: item.opening_key,
            playerColor,
          },
        },
      });
    },
    [navigate, playerColor],
  );

  // Mirror the exact-best projection GameAnalysisPage applies at the same seam
  // (g-kfxj, g-22t8.2): a played move equal to the TRUSTED position best is promoted
  // to 'best' (loss 0). Project once here and feed the result to every consumer so the
  // review stats agree with the board on THOSE promotions — the board re-projects
  // internally, so an unprojected stats pane was the only side left disagreeing about
  // them. Board-only re-annotation overlays (`upgraded`) are a separate grain and can
  // still star a move the pane does not count; that is by design, not this seam's job.
  const projectedMoves = useMemo(
    () =>
      analysis
        ? projectExactBest(analysis.moves, analysis.position_analysis)
        : null,
    [analysis],
  );

  const { sideStats, highlightedMoves, handleStatHover, handleStatClick, handleGraphMoveClick, pinnedStat, activeStat } =
    useGameReviewStats({
      selectedId,
      moves: projectedMoves,
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
          {!isNarrow && gameSelector}

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

                {!analysisLoading && analysis && sideStats && projectedMoves && (
                  <AnalysisBoard
                    ref={boardRef}
                    key={selectedGame.session_id}
                    moves={projectedMoves}
                    boardOrientation={playerColor}
                    sessionId={selectedGame.session_id}
                    initialMoveIndex={analysis.moves.length > 0 ? 0 : undefined}
                    positionAnalysis={analysis.position_analysis}
                    highlightedMoves={highlightedMoves}
                    onGraphMoveClick={handleGraphMoveClick}
                    mobileToolbar={isNarrow ? gameSelector : undefined}
                    footer={
                      <>
                        <GameReviewStats
                          sideStats={sideStats}
                          activeStat={activeStat}
                          pinnedStat={pinnedStat}
                          totalMoves={analysis.moves.length}
                          accuracy={analysis.summary.accuracy}
                          accuracyPending={analysisProcessing}
                          onStatHover={handleStatHover}
                          onStatClick={handleStatClick}
                        />
                        <GameOpeningLineage
                          playerColor={playerColor}
                          lineage={openingLineage}
                          startPly={openingStartPly}
                          onSelectRoot={handleSelectRoot}
                          onStartDrill={handleStartDrill}
                        />
                      </>
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
                      <span className="analysis-stat__value">{selectedGame.summary.average_centipawn_loss ?? '—'}</span>
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
