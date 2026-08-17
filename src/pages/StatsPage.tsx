import { useEffect, useState } from "react";
import {
  getStatsSummary,
  type StatsSummaryResponse,
  type StatsWindowDays,
} from "../utils/api";
import AppNav from "../components/AppNav";
import RatingGraph from "../components/RatingGraph";
import { accuracyColor } from "../utils/statColor";
import { formatScore } from "../openings/format";
import { captureEvent } from "../analytics/posthog";
import "./StatsPage.css";

const WINDOW_OPTIONS: Array<{ label: string; value: StatsWindowDays }> = [
  { label: "7d", value: 7 },
  { label: "30d", value: 30 },
  { label: "90d", value: 90 },
  { label: "365d", value: 365 },
  { label: "All", value: 0 },
];

// Best/Excellent/Good buckets are intentionally dropped — the distribution now
// surfaces only the three mistake grades.
const QUALITY_KEYS = [
  { key: "inaccuracy", label: "Inaccuracy" },
  { key: "mistake", label: "Mistake" },
  { key: "blunder", label: "Blunder" },
] as const;

// Rate/percentage fields are `number | null`; null (empty denominator) renders as
// an em dash rather than a misleading 0.0%.
function formatPercent(value: number | null): string {
  return value == null ? "—" : `${value.toFixed(1)}%`;
}

function formatAverage(value: number): string {
  return value.toFixed(1);
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
  return data.games.played === 0 && data.library.blunders_total === 0;
}

function StatsPage() {
  const [windowDays, setWindowDays] = useState<StatsWindowDays>(30);
  const [presetKey, setPresetKey] = useState(0);
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
          setError(
            err instanceof Error ? err.message : "Failed to load stats summary",
          );
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

  const openingsEmpty =
    summary != null &&
    summary.openings.strongest.length === 0 &&
    summary.openings.weakest.length === 0;

  return (
    <main className="app-shell">
      <AppNav />

      <div className="constrained-content">
        <section className="stats-shell">
          <header className="stats-shell__header">
            <h1 className="stats-shell__title">Your Stats</h1>
            <p className="stats-shell__hint">
              Performance summary for recent games and your blunder library.
            </p>
          </header>

          <div
            className="stats-window-picker"
            role="group"
            aria-label="Stats window"
          >
            {WINDOW_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                className={`stats-window-picker__button${windowDays === option.value ? " stats-window-picker__button--active" : ""}`}
                aria-pressed={windowDays === option.value}
                onClick={() => {
                  if (windowDays !== option.value) {
                    captureEvent("stats_window_changed", {
                      window_days: option.value,
                    });
                    setLoading(true);
                    setError(null);
                    setWindowDays(option.value);
                  }
                  setPresetKey((k) => k + 1);
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

          <RatingGraph windowDays={windowDays} presetKey={presetKey} />

          {!loading && !error && summary && (
            <>
              {isEmptySummary(summary) && (
                <p className="stats-shell__empty">
                  No games in this window yet. Play a game to start building
                  stats.
                </p>
              )}

              <section className="stats-section">
                <h2 className="stats-section__title">Results</h2>
                <div className="stats-grid stats-grid--three">
                  <article className="stats-card">
                    <p className="stats-card__label">Score</p>
                    <p className="stats-card__value">
                      {formatPercent(summary.games.score_pct)}
                    </p>
                    <p className="stats-card__inline">
                      {summary.games.wins}–{summary.games.losses}–
                      {summary.games.draws} W–L–D
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Played</p>
                    <p className="stats-card__value">{summary.games.played}</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Avg Moves</p>
                    <p className="stats-card__value">
                      {formatAverage(summary.games.avg_moves)}
                    </p>
                  </article>
                </div>
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">Accuracy &amp; Mistakes</h2>
                <div className="stats-grid stats-grid--two">
                  <article className="stats-card">
                    <p className="stats-card__label">Accuracy</p>
                    <p
                      className="stats-card__value"
                      style={
                        summary.moves.accuracy_pct != null
                          ? { color: accuracyColor(summary.moves.accuracy_pct) }
                          : undefined
                      }
                    >
                      {formatPercent(summary.moves.accuracy_pct)}
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Mistake-Free Games</p>
                    <p className="stats-card__value">
                      {formatPercent(summary.moves.mistake_free_game_rate)}
                    </p>
                  </article>
                </div>
                {summary.moves.quality_distribution ? (
                  <div
                    className="stats-quality-list"
                    role="list"
                    aria-label="Move quality distribution"
                  >
                    {QUALITY_KEYS.map((item) => (
                      <div
                        key={item.key}
                        className="stats-quality-item"
                        role="listitem"
                      >
                        <span className="stats-quality-item__label">
                          {item.label}
                        </span>
                        <span className="stats-quality-item__value">
                          {formatPercent(
                            summary.moves.quality_distribution![item.key],
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="stats-list-card__empty">
                    No analyzed moves in this window yet.
                  </p>
                )}
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">By Color</h2>
                <div className="stats-grid stats-grid--two">
                  <article className="stats-card">
                    <p className="stats-card__label">White</p>
                    <p className="stats-card__inline">
                      Games: {summary.colors.white.games}
                    </p>
                    <p className="stats-card__inline">
                      Score: {formatPercent(summary.colors.white.score_pct)}
                    </p>
                    <p className="stats-card__inline">
                      Accuracy:{" "}
                      {formatPercent(summary.colors.white.accuracy_pct)}
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Black</p>
                    <p className="stats-card__inline">
                      Games: {summary.colors.black.games}
                    </p>
                    <p className="stats-card__inline">
                      Score: {formatPercent(summary.colors.black.score_pct)}
                    </p>
                    <p className="stats-card__inline">
                      Accuracy:{" "}
                      {formatPercent(summary.colors.black.accuracy_pct)}
                    </p>
                  </article>
                </div>
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">Training</h2>
                <div className="stats-grid stats-grid--three">
                  <article className="stats-card">
                    <p className="stats-card__label">Review Retention</p>
                    <p className="stats-card__value">
                      {formatPercent(summary.training.retention_pct)}
                    </p>
                    <p className="stats-card__inline">
                      {summary.training.retained_blunders}/
                      {summary.training.reviewed_blunders} held · All-time
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Review Pass Rate</p>
                    <p className="stats-card__value">
                      {formatPercent(summary.training.review_pass_rate)}
                    </p>
                    <p className="stats-card__inline">
                      {summary.training.reviews_passed}/
                      {summary.training.reviews_total} reviews
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Blunders Mastered</p>
                    <p className="stats-card__value">
                      {summary.training.conversions_in_window}
                    </p>
                    <p className="stats-card__inline">
                      Reached {summary.training.mastery_threshold}-pass streak
                    </p>
                  </article>
                </div>
              </section>

              <section className="stats-section">
                <h2 className="stats-section__title">Library</h2>
                <div className="stats-grid stats-grid--three">
                  <article className="stats-card">
                    <p className="stats-card__label">New Blunders</p>
                    <p className="stats-card__value">
                      {summary.library.new_blunders_in_window}
                    </p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Blunders Total</p>
                    <p className="stats-card__value">
                      {summary.library.blunders_total}
                    </p>
                    <p className="stats-card__inline">All-time</p>
                  </article>
                  <article className="stats-card">
                    <p className="stats-card__label">Avg Blunder Loss (cp)</p>
                    <p className="stats-card__value">
                      {summary.library.avg_blunder_eval_loss_cp}
                    </p>
                    <p className="stats-card__inline">All-time</p>
                  </article>
                </div>
                <div className="stats-list-card">
                  <h3 className="stats-list-card__title">
                    Top Costly Blunders
                    <span className="stats-card__inline"> · All-time</span>
                  </h3>
                  {summary.library.top_costly_blunders.length === 0 ? (
                    <p className="stats-list-card__empty">
                      No blunders captured yet.
                    </p>
                  ) : (
                    <ul className="stats-list-card__list">
                      {summary.library.top_costly_blunders.map((blunder) => (
                        <li
                          key={blunder.blunder_id}
                          className="stats-list-card__item"
                        >
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

              {!openingsEmpty && (
                <section className="stats-section">
                  <h2 className="stats-section__title">
                    Openings
                    <span className="stats-card__inline"> · All-time</span>
                  </h2>
                  <div className="stats-grid stats-grid--two">
                    <div className="stats-list-card">
                      <h3 className="stats-list-card__title">Strongest</h3>
                      {summary.openings.strongest.length === 0 ? (
                        <p className="stats-list-card__empty">
                          Not enough data yet.
                        </p>
                      ) : (
                        <ul className="stats-list-card__list">
                          {summary.openings.strongest.map((opening) => (
                            <li
                              key={`${opening.player_color}-${opening.opening_name}`}
                              className="stats-list-card__item"
                            >
                              <span>{opening.opening_name}</span>
                              <span>{opening.player_color}</span>
                              <span>{formatScore(opening.opening_score)}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                    <div className="stats-list-card">
                      <h3 className="stats-list-card__title">Weakest</h3>
                      {summary.openings.weakest.length === 0 ? (
                        <p className="stats-list-card__empty">
                          Not enough data yet.
                        </p>
                      ) : (
                        <ul className="stats-list-card__list">
                          {summary.openings.weakest.map((opening) => (
                            <li
                              key={`${opening.player_color}-${opening.opening_name}`}
                              className="stats-list-card__item"
                            >
                              <span>{opening.opening_name}</span>
                              <span>{opening.player_color}</span>
                              <span>{formatScore(opening.opening_score)}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>
                </section>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}

export default StatsPage;
