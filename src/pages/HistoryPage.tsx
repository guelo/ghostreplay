import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { fetchHistory, fetchAnalysis, type HistoryGame, type SessionAnalysis } from "../utils/api";
import AnalysisBoard from "../components/AnalysisBoard";
import AppNav from "../components/AppNav";
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

function resultClass(result: string | null): string {
  switch (result) {
    case "checkmate_win":
      return "game-card__result--win";
    case "checkmate_loss":
    case "resign":
      return "game-card__result--loss";
    default:
      return "game-card__result--other";
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

function HistoryPage() {
  const location = useLocation();
  const state = location.state as {
    select?: "latest";
    source?: string;
  } | null;

  const [games, setGames] = useState<HistoryGame[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<SessionAnalysis | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchHistory()
      .then((data) => {
        if (cancelled) return;
        setGames(data);
        if (data.length > 0) {
          setAnalysisLoading(true);
          setAnalysis(null);
          setSelectedId(data[0].session_id);
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
  }, []);

  useEffect(() => {
    if (!selectedId) {
      return;
    }
    let cancelled = false;
    fetchAnalysis(selectedId)
      .then((data) => {
        if (!cancelled) setAnalysis(data);
      })
      .catch(() => {
        // Analysis fetch failed — pane will fall back to summary from history list
      })
      .finally(() => {
        if (!cancelled) setAnalysisLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const selectedGame = games.find((g) => g.session_id === selectedId) ?? null;

  return (
    <main className="app-shell">
      <AppNav />

      <div className="constrained-content">
        <section className="history-shell">
          <div className="history-shell__header">
            <h1 className="history-shell__title">Game History</h1>
            {state?.select === "latest" && (
              <p className="history-shell__hint">
                {state.source === "post_game_view_analysis"
                  ? "Showing analysis for your latest game."
                  : "Browsing your game history."}
              </p>
            )}
          </div>

          {loading && (
            <p className="history-shell__placeholder">Loading games...</p>
          )}

          {error && (
            <p className="history-shell__error">{error}</p>
          )}

          {!loading && !error && games.length === 0 && (
            <div className="history-shell__empty">
              <p className="history-shell__placeholder">No games yet.</p>
              <Link to="/game" className="chess-button primary">
                Play a Game
              </Link>
            </div>
          )}

          {!loading && !error && games.length > 0 && (
            <div className="history-layout">
              <ul className="game-list" role="listbox" aria-label="Game history">
                {games.map((game) => (
                  <li key={game.session_id}>
                    <button
                      className={`game-card${game.session_id === selectedId ? " game-card--selected" : ""}`}
                      type="button"
                      role="option"
                      aria-selected={game.session_id === selectedId}
                      onClick={() => {
                        if (game.session_id === selectedId) {
                          return;
                        }
                        setAnalysisLoading(true);
                        setAnalysis(null);
                        setSelectedId(game.session_id);
                      }}
                    >
                      <div className="game-card__top">
                        <span className={`game-card__result ${resultClass(game.result)}`}>
                          {resultLabel(game.result)}
                        </span>
                        <span className="game-card__elo">{game.engine_elo}</span>
                      </div>
                      <div className="game-card__meta">
                        <span className="game-card__color">
                          {game.player_color === "white" ? "\u2659" : "\u265F"} {game.player_color}
                        </span>
                        <span className="game-card__date">
                          {game.ended_at ? formatDate(game.ended_at) : "In progress"}
                        </span>
                      </div>
                      <div className="game-card__stats">
                        <span className="game-card__stat">{game.summary.total_moves} moves</span>
                        {game.summary.blunders > 0 && (
                          <span className="game-card__stat game-card__stat--blunder">
                            {game.summary.blunders} blunder{game.summary.blunders !== 1 ? "s" : ""}
                          </span>
                        )}
                        {game.summary.mistakes > 0 && (
                          <span className="game-card__stat game-card__stat--mistake">
                            {game.summary.mistakes} mistake{game.summary.mistakes !== 1 ? "s" : ""}
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>

              <div className="analysis-pane">
                {selectedGame ? (
                  <div className="analysis-pane__shell">
                    <h2 className="analysis-pane__title">
                      {resultLabel(selectedGame.result)} vs Engine {selectedGame.engine_elo}
                    </h2>
                    <p className="analysis-pane__subtitle">
                      {selectedGame.player_color === "white" ? "\u2659" : "\u265F"}{" "}
                      Playing {selectedGame.player_color}
                      {selectedGame.ended_at && <> &middot; {formatDate(selectedGame.ended_at)}</>}
                    </p>

                    {analysisLoading && (
                      <p className="analysis-pane__placeholder">Loading analysis...</p>
                    )}

                    {!analysisLoading && analysis && (
                      <>
                        <div className="analysis-pane__summary">
                          <div className="analysis-stat">
                            <span className="analysis-stat__value">{analysis.moves.length}</span>
                            <span className="analysis-stat__label">Moves</span>
                          </div>
                          <div className="analysis-stat">
                            <span className="analysis-stat__value">{analysis.summary.blunders}</span>
                            <span className="analysis-stat__label">Blunders</span>
                          </div>
                          <div className="analysis-stat">
                            <span className="analysis-stat__value">{analysis.summary.mistakes}</span>
                            <span className="analysis-stat__label">Mistakes</span>
                          </div>
                          <div className="analysis-stat">
                            <span className="analysis-stat__value">{analysis.summary.inaccuracies}</span>
                            <span className="analysis-stat__label">Inaccuracies</span>
                          </div>
                          <div className="analysis-stat">
                            <span className="analysis-stat__value">{analysis.summary.average_centipawn_loss}</span>
                            <span className="analysis-stat__label">Avg CPL</span>
                          </div>
                        </div>

                        <AnalysisBoard
                          moves={analysis.moves}
                          boardOrientation={selectedGame.player_color as "white" | "black"}
                        />
                      </>
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
                ) : (
                  <p className="analysis-pane__placeholder">
                    Select a game to view analysis.
                  </p>
                )}
              </div>
            </div>
          )}

          <Link to="/" className="chess-button secondary history-shell__back">
            Back to Game
          </Link>
        </section>
      </div>
    </main>
  );
}

export default HistoryPage;
