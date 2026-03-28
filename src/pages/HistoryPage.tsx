import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  fetchHistory,
  fetchAnalysis,
  type AnalysisMove,
  type HistoryGame,
  type SessionAnalysis,
} from "../utils/api";
import type { OpenHistoryOptions } from "../components/chess-game/types";
import AnalysisBoard from "../components/AnalysisBoard";
import AppNav from "../components/AppNav";
import { useTouchOnly } from "../hooks/useTouchOnly";
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

type ClassKey = 'blunder' | 'mistake' | 'inaccuracy';
type SideStats = Record<ClassKey, { count: number; indices: number[] }> & { avgCpl: number };
type StatSelection = { side: 'player' | 'opponent'; cls: ClassKey } | null;

const CLASS_KEYS: ClassKey[] = ['blunder', 'mistake', 'inaccuracy'];

function computeSideStats(
  moves: AnalysisMove[],
  playerColor: 'white' | 'black',
): { player: SideStats; opponent: SideStats } {
  const makeSide = (): SideStats => ({
    blunder: { count: 0, indices: [] },
    mistake: { count: 0, indices: [] },
    inaccuracy: { count: 0, indices: [] },
    avgCpl: 0,
  });
  const player = makeSide();
  const opponent = makeSide();

  let playerDeltaSum = 0, playerDeltaCount = 0;
  let opponentDeltaSum = 0, opponentDeltaCount = 0;

  for (let i = 0; i < moves.length; i++) {
    const m = moves[i];
    const isPlayer = m.color === playerColor;
    const side = isPlayer ? player : opponent;
    const cls = m.classification as ClassKey | null;
    if (cls && cls in side) {
      side[cls].count++;
      side[cls].indices.push(i);
    }
    if (m.eval_delta != null) {
      if (isPlayer) {
        playerDeltaSum += m.eval_delta;
        playerDeltaCount++;
      } else {
        opponentDeltaSum += m.eval_delta;
        opponentDeltaCount++;
      }
    }
  }
  player.avgCpl = playerDeltaCount > 0 ? Math.round(playerDeltaSum / playerDeltaCount) : 0;
  opponent.avgCpl = opponentDeltaCount > 0 ? Math.round(opponentDeltaSum / opponentDeltaCount) : 0;

  return { player, opponent };
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
          // Honor sessionId from navigation state, or fall back to first game
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
  // navState is only read on initial mount
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch analysis and poll while incomplete
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
            // Refetch history summary once complete to sync counts
            fetchHistory()
              .then((fresh) => { if (!cancelled) setGames(fresh); })
              .catch(() => {});
          } else {
            // Poll cap reached but still incomplete — keep the banner
            // so the user knows the data is not final.
            setAnalysisProcessing(true);
          }
        })
        .catch(() => {
          if (cancelled) return;
          if (isInitial) setAnalysisLoading(false);
          // Keep polling after transient errors so we don't get stuck
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

  const [statInteraction, setStatInteraction] = useState<{
    gameId: string | null;
    pinned: StatSelection;
    hovered: StatSelection;
  }>({ gameId: null, pinned: null, hovered: null });

  // Reset interaction state when game changes (derived state pattern)
  const pinnedStat = statInteraction.gameId === selectedId ? statInteraction.pinned : null;
  const hoveredStat = statInteraction.gameId === selectedId ? statInteraction.hovered : null;

  const setPinnedStat = useCallback((pinned: StatSelection) => {
    setStatInteraction(prev => ({ ...prev, gameId: selectedId, pinned }));
  }, [selectedId]);

  const setHoveredStat = useCallback((hovered: StatSelection) => {
    setStatInteraction(prev => ({ ...prev, gameId: selectedId, hovered }));
  }, [selectedId]);

  const isTouchOnly = useTouchOnly();

  const selectedGame = games.find((g) => g.session_id === selectedId) ?? null;
  const playerColor = (selectedGame?.player_color as 'white' | 'black') ?? 'white';

  const sideStats = useMemo(() => {
    if (!analysis) return null;
    return computeSideStats(analysis.moves, playerColor);
  }, [analysis, playerColor]);

  const activeStat = hoveredStat ?? pinnedStat;

  const highlightedMoves = useMemo(() => {
    if (!activeStat || !sideStats) return null;
    const stats = activeStat.side === 'player' ? sideStats.player : sideStats.opponent;
    return { indices: stats[activeStat.cls].indices, classification: activeStat.cls };
  }, [activeStat, sideStats]);

  const handleStatHover = useCallback((sel: StatSelection) => {
    if (isTouchOnly) return;
    setHoveredStat(sel);
  }, [isTouchOnly, setHoveredStat]);

  const handleStatClick = useCallback((sel: StatSelection) => {
    const isToggleOff = pinnedStat?.side === sel?.side && pinnedStat?.cls === sel?.cls;
    setPinnedStat(isToggleOff ? null : sel);
  }, [pinnedStat, setPinnedStat]);

  const handleGraphMoveClick = useCallback(() => {
    setPinnedStat(null);
  }, [setPinnedStat]);

  return (
    <main className="app-shell history-page">
      <AppNav />

      <div className="constrained-content">
        <section className="history-shell">
          {games.length > 0 && (
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
              <Link to="/game" className="chess-button primary">
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
                    key={selectedGame.session_id}
                    moves={analysis.moves}
                    boardOrientation={playerColor}
                    positionAnalysis={analysis.position_analysis}
                    highlightedMoves={highlightedMoves}
                    onGraphMoveClick={handleGraphMoveClick}
                    footer={
                      <div className="history-stats-pane">
                        <div className="history-stats-pane__grid">
                          {/* Header row */}
                          <div className="history-stats-pane__header" />
                          <div className="history-stats-pane__header">You</div>
                          <div className="history-stats-pane__header">Ghost</div>

                          {/* Classification rows */}
                          {CLASS_KEYS.map((cls) => {
                            const label = cls === 'inaccuracy' ? 'Inaccur.' : cls.charAt(0).toUpperCase() + cls.slice(1) + 's';
                            const fullLabel = cls === 'inaccuracy' ? 'Inaccuracies' : cls.charAt(0).toUpperCase() + cls.slice(1) + 's';
                            const labelSel = { side: 'player' as const, cls };
                            const isLabelActive = activeStat?.cls === cls;
                            const isLabelPressed = pinnedStat?.side === 'player' && pinnedStat?.cls === cls;
                            return [
                              <button
                                key={`${cls}-label`}
                                type="button"
                                aria-label={`Your ${fullLabel}`}
                                aria-pressed={isLabelPressed}
                                className={`history-stats-pane__label history-stats-pane__label--${cls} history-stats-pane__label--interactive${isLabelActive ? ' history-stats-pane__label--active' : ''}`}
                                onMouseEnter={() => handleStatHover(labelSel)}
                                onMouseLeave={() => handleStatHover(null)}
                                onClick={() => handleStatClick(labelSel)}
                              >
                                {label}
                              </button>,
                              ...(['player', 'opponent'] as const).map((side) => {
                                const sel = { side, cls };
                                const isActive = activeStat?.side === side && activeStat?.cls === cls;
                                const isPressed = pinnedStat?.side === side && pinnedStat?.cls === cls;
                                const sideLabel = side === 'player' ? 'Your' : 'Ghost';
                                return (
                                  <button
                                    key={`${cls}-${side}`}
                                    type="button"
                                    aria-label={`${sideLabel} ${fullLabel}: ${sideStats[side][cls].count}`}
                                    aria-pressed={isPressed}
                                    className={`history-stats-pane__value history-stats-pane__value--${cls} history-stats-pane__value--interactive${isActive ? ' history-stats-pane__value--active' : ''}`}
                                    onMouseEnter={() => handleStatHover(sel)}
                                    onMouseLeave={() => handleStatHover(null)}
                                    onClick={() => handleStatClick(sel)}
                                  >
                                    {sideStats[side][cls].count}
                                  </button>
                                );
                              }),
                            ];
                          })}

                          {/* Avg CPL row */}
                          <div className="history-stats-pane__label">Avg CPL</div>
                          <div className="history-stats-pane__value">{sideStats.player.avgCpl}</div>
                          <div className="history-stats-pane__value">{sideStats.opponent.avgCpl}</div>

                          {/* Moves row */}
                          <div className="history-stats-pane__label">Moves</div>
                          <div className="history-stats-pane__value history-stats-pane__value--span">{analysis.moves.length}</div>
                        </div>
                      </div>
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
