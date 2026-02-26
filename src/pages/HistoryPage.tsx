import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchHistory,
  fetchAnalysis,
  type HistoryGame,
  type SessionAnalysis,
} from "../utils/api";
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

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function HistoryPage() {
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

                {!analysisLoading && analysis && (
                  <AnalysisBoard
                    moves={analysis.moves}
                    boardOrientation={
                      selectedGame.player_color as "white" | "black"
                    }
                    footer={
                      <div className="analysis-pane__summary">
                        <div className="analysis-stat">
                          <span className="analysis-stat__value">
                            {analysis.moves.length}
                          </span>
                          <span className="analysis-stat__label">Moves</span>
                        </div>
                        <div className="analysis-stat">
                          <span className="analysis-stat__value">
                            {analysis.summary.blunders}
                          </span>
                          <span className="analysis-stat__label">Blunders</span>
                        </div>
                        <div className="analysis-stat">
                          <span className="analysis-stat__value">
                            {analysis.summary.mistakes}
                          </span>
                          <span className="analysis-stat__label">Mistakes</span>
                        </div>
                        <div className="analysis-stat">
                          <span className="analysis-stat__value">
                            {analysis.summary.inaccuracies}
                          </span>
                          <span className="analysis-stat__label">
                            Inaccuracies
                          </span>
                        </div>
                        <div className="analysis-stat">
                          <span className="analysis-stat__value">
                            {analysis.summary.average_centipawn_loss}
                          </span>
                          <span className="analysis-stat__label">Avg CPL</span>
                        </div>
                      </div>
                    }
                  />
                )}

                {!analysisLoading && !analysis && (
                  <div className="analysis-pane__summary">
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">
                        {selectedGame.summary.total_moves}
                      </span>
                      <span className="analysis-stat__label">Moves</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">
                        {selectedGame.summary.blunders}
                      </span>
                      <span className="analysis-stat__label">Blunders</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">
                        {selectedGame.summary.mistakes}
                      </span>
                      <span className="analysis-stat__label">Mistakes</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">
                        {selectedGame.summary.inaccuracies}
                      </span>
                      <span className="analysis-stat__label">Inaccuracies</span>
                    </div>
                    <div className="analysis-stat">
                      <span className="analysis-stat__value">
                        {selectedGame.summary.average_centipawn_loss}
                      </span>
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
