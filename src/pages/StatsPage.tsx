import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../contexts/useAuth";
import {
  getStatsSummary,
  type StatsSummaryResponse,
  type StatsWindowDays,
} from "../utils/api";
import "../App.css";

const WINDOW_OPTIONS: Array<{ label: string; value: StatsWindowDays }> = [
  { label: "7d", value: 7 },
  { label: "30d", value: 30 },
  { label: "90d", value: 90 },
  { label: "365d", value: 365 },
  { label: "All", value: 0 },
];

const QUALITY_KEYS = [
  { key: "best", label: "Best" },
  { key: "excellent", label: "Excellent" },
  { key: "good", label: "Good" },
  { key: "inaccuracy", label: "Inaccuracy" },
  { key: "mistake", label: "Mistake" },
  { key: "blunder", label: "Blunder" },
] as const;

function formatPercent(value: number): string {
  return `${value.toFixed(1)}%`;
}

function formatAverage(value: number): string {
  return value.toFixed(1);
}

function formatDuration(seconds: number): string {
  if (seconds <= 0) {
    return "0m 0s";
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m ${remainingSeconds}s`;
}

function formatDate(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function isEmptySummary(data: StatsSummaryResponse): boolean {
  return (
    data.games.played === 0 &&
    data.moves.player_moves === 0 &&
    data.library.blunders_total === 0
  );
}

function StatsPage() {
  const { user, logout } = useAuth();
  const [windowDays, setWindowDays] = useState<StatsWindowDays>(30);
  const [summary, setSummary] = useState<StatsSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    let cancelled = false;

    getStatsSummary(windowDays)
      .then((data) => {
        if (!cancelled) {
          setError(null);
          setSummary(data);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setSummary(null);
          setError(err instanceof Error ? err.message : "Failed to load stats summary");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [windowDays, retryCount]);

  return (
    <main className="app-shell">
      <nav className="nav-bar">
        <Link to="/" className="nav-bar__brand">
          Ghost Replay
        </Link>
        <div className="nav-bar__actions">
          <Link to="/history" className="nav-bar__link">History</Link>
          <Link to="/stats" className="nav-bar__link">Stats</Link>
          {user?.isAnonymous ? (
            <>
              <Link to="/register" className="chess-button primary nav-bar__btn">
                Register
              </Link>
              <Link to="/login" className="nav-bar__link">
                Log in
              </Link>
            </>
          ) : (
            <>
              <span className="nav-bar__user">{user?.username}</span>
              <button
                className="nav-bar__link"
                type="button"
                onClick={logout}
              >
                Log out
              </button>
            </>
          )}
        </div>
      </nav>

      <div className="constrained-content">
        <section className="stats-shell">
          <header className="stats-shell__header">
            <h1 className="stats-shell__title">Your Stats</h1>
            <p className="stats-shell__hint">
              Performance summary for recent games and your blunder library.
            </p>
          </header>

          <div className="stats-window-picker" role="group" aria-label="Stats window">
            {WINDOW_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                className={`stats-window-picker__button${windowDays === option.value ? " stats-window-picker__button--active" : ""}`}
                aria-pressed={windowDays === option.value}
                onClick={() => {
                  setLoading(true);
                  setError(null);
                  setWindowDays(option.value);
                }}
              >
                {option.label}
              </button>
            ))}
          </div>

          {loading && (
            <p className="stats-shell__placeholder">Loading stats...</p>
          )}

          {!loading && error && (
            <div className="stats-shell__error">
              <p>{error}</p>
              <button
                className="chess-button primary"
                type="button"
                onClick={() => {
                  setLoading(true);
                  setError(null);
                  setRetryCount((value) => value + 1);
                }}
              >
                Retry
              </button>
            </div>
          )}

          {!loading && !error && summary && (
            <>
              {isEmptySummary(summary) && (
                <p className="stats-shell__empty">
                  No games in this window yet. Play a game to start building stats.
                </p>
              )}

              <section className="stats-section">
                <h2 className="stats-section__title">Games</h2>
                <div className="stats-grid">
                  <article className="stats-card">
                    <p className="stats-card__label">Played</p>
                    <p className="stats-card__value">{summary.games.played}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Completed</p>
                    <p className="stats-card__value">{summary.games.completed}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Active</p>
                    <p className="stats-card__value">{summary.games.active}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Wins</p>
                    <p className="stats-card__value">{summary.games.record.wins}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Losses</p>
                    <p className="stats-card__value">{summary.games.record.losses}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Draws</p>
                    <p className="stats-card__value">{summary.games.record.draws}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Resigns</p>
                    <p className="stats-card__value">{summary.games.record.resigns}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Abandons</p>
                    <p className="stats-card__value">{summary.games.record.abandons}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Avg Duration</p>
                    <p className="stats-card__value">
                      {formatDuration(summary.games.avg_duration_seconds)}
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Avg Moves</p>
                    <p className="stats-card__value">{formatAverage(summary.games.avg_moves)}</p>
                  </article>
                </div>
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">Move Quality</h2>
                <div className="stats-grid stats-grid--four">
                  <article className="stats-card">
                    <p className="stats-card__label">Player Moves</p>
                    <p className="stats-card__value">{summary.moves.player_moves}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Avg CPL</p>
                    <p className="stats-card__value">{formatAverage(summary.moves.avg_cpl)}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Mistakes / 100</p>
                    <p className="stats-card__value">
                      {formatPercent(summary.moves.mistakes_per_100_moves)}
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Blunders / 100</p>
                    <p className="stats-card__value">
                      {formatPercent(summary.moves.blunders_per_100_moves)}
                    </p>
                  </article>
                </div>
                <div className="stats-quality-list" role="list" aria-label="Move quality distribution">
                  {QUALITY_KEYS.map((item) => (
                    <div key={item.key} className="stats-quality-item" role="listitem">
                      <span className="stats-quality-item__label">{item.label}</span>
                      <span className="stats-quality-item__value">
                        {formatPercent(summary.moves.quality_distribution[item.key])}
                      </span>
                    </div>
                  ))}
                </div>
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">By Color</h2>
                <div className="stats-grid stats-grid--two">
                  <article className="stats-card">
                    <p className="stats-card__label">White</p>
                    <p className="stats-card__inline">Games: {summary.colors.white.games}</p>
                    <p className="stats-card__inline">Avg CPL: {formatAverage(summary.colors.white.avg_cpl)}</p>
                    <p className="stats-card__inline">
                      Blunders / 100: {formatPercent(summary.colors.white.blunders_per_100_moves)}
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Black</p>
                    <p className="stats-card__inline">Games: {summary.colors.black.games}</p>
                    <p className="stats-card__inline">Avg CPL: {formatAverage(summary.colors.black.avg_cpl)}</p>
                    <p className="stats-card__inline">
                      Blunders / 100: {formatPercent(summary.colors.black.blunders_per_100_moves)}
                    </p>
                  </article>
                </div>
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">Library</h2>
                <div className="stats-grid stats-grid--three">
                  <article className="stats-card">
                    <p className="stats-card__label">Blunders Total</p>
                    <p className="stats-card__value">{summary.library.blunders_total}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Positions Total</p>
                    <p className="stats-card__value">{summary.library.positions_total}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Edges Total</p>
                    <p className="stats-card__value">{summary.library.edges_total}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">New Blunders</p>
                    <p className="stats-card__value">{summary.library.new_blunders_in_window}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Avg Blunder Loss (cp)</p>
                    <p className="stats-card__value">{summary.library.avg_blunder_eval_loss_cp}</p>
                  </article>
                </div>
                <div className="stats-list-card">
                  <h3 className="stats-list-card__title">Top Costly Blunders</h3>
                  {summary.library.top_costly_blunders.length === 0 ? (
                    <p className="stats-list-card__empty">No blunders captured yet.</p>
                  ) : (
                    <ul className="stats-list-card__list">
                      {summary.library.top_costly_blunders.map((blunder) => (
                        <li key={blunder.blunder_id} className="stats-list-card__item">
                          <span>
                            {blunder.bad_move_san} vs {blunder.best_move_san}
                          </span>
                          <span>{blunder.eval_loss_cp} cp</span>
                          <span>{formatDate(blunder.created_at)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">Data Completeness</h2>
                <div className="stats-list-card">
                  <p className="stats-card__inline">
                    Sessions with uploaded moves:{" "}
                    <strong>{formatPercent(summary.data_completeness.sessions_with_uploaded_moves_pct)}</strong>
                  </p>
                  <ul className="stats-notes">
                    {summary.data_completeness.notes.map((note) => (
                      <li key={note}>{note}</li>
                    ))}
                  </ul>
                </div>
              </section>
            </>
          )}

          <Link to="/" className="chess-button secondary stats-shell__back">
            Back to Game
          </Link>
        </section>
      </div>
    </main>
  );
}

export default StatsPage;
