import { useEffect, useRef, useState } from "react";
import AppNav from "../components/AppNav";
import {
  getOpeningFamilyScores,
  type FamilyScoresResponse,
  type OpeningPlayerColor,
} from "../utils/api";
import "../App.css";

const COLOR_OPTIONS: Array<{ label: string; value: OpeningPlayerColor }> = [
  { label: "White", value: "white" },
  { label: "Black", value: "black" },
];

const LOADING_CARD_COUNT = 3;

function getColorLabel(playerColor: OpeningPlayerColor): string {
  return playerColor === "white" ? "White" : "Black";
}

function formatScore(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function normalizePercentValue(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }

  const normalizedValue = value <= 1 ? value * 100 : value;
  return Math.min(100, Math.max(0, normalizedValue));
}

function formatPercent(value: number): string {
  return `${Math.round(normalizePercentValue(value))}%`;
}

function formatGames(value: number): string {
  return value.toLocaleString();
}

function getPriorityTone(score: number): "alert" | "watch" | "steady" {
  if (score < 45) {
    return "alert";
  }

  if (score < 65) {
    return "watch";
  }

  return "steady";
}

function getPriorityLabel(score: number): string {
  const tone = getPriorityTone(score);

  switch (tone) {
    case "alert":
      return "Fix First";
    case "watch":
      return "Study Next";
    default:
      return "Holding";
  }
}

function formatRootCount(rootCount: number): string {
  return `${rootCount} ${rootCount === 1 ? "root" : "roots"}`;
}

function sortFamiliesByStrength(
  families: FamilyScoresResponse["families"],
): FamilyScoresResponse["families"] {
  return [...families].sort((left, right) => {
    if (left.weakest_root_score !== right.weakest_root_score) {
      return right.weakest_root_score - left.weakest_root_score;
    }

    if (left.family_score !== right.family_score) {
      return right.family_score - left.family_score;
    }

    return left.family_name.localeCompare(right.family_name);
  });
}

function OpeningsPage() {
  const [playerColor, setPlayerColor] = useState<OpeningPlayerColor>("white");
  const [response, setResponse] = useState<FamilyScoresResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const requestVersionRef = useRef(0);

  const invalidatePendingRequests = () => {
    requestVersionRef.current += 1;
  };

  useEffect(() => {
    let active = true;
    const requestVersion = requestVersionRef.current + 1;
    requestVersionRef.current = requestVersion;

    setLoading(true);
    setError(null);
    setResponse(null);

    getOpeningFamilyScores(playerColor)
      .then((data) => {
        if (!active || requestVersionRef.current !== requestVersion) {
          return;
        }

        setResponse(data);
      })
      .catch((err: unknown) => {
        if (!active || requestVersionRef.current !== requestVersion) {
          return;
        }

        setResponse(null);
        setError(
          err instanceof Error ? err.message : "Failed to load opening families",
        );
      })
      .finally(() => {
        if (!active || requestVersionRef.current !== requestVersion) {
          return;
        }

        setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [playerColor, retryCount]);

  const selectedColorLabel = getColorLabel(playerColor);
  const hasFamilies = Boolean(response && response.families.length > 0);
  const isTrueNoEvidence =
    response !== null &&
    response.computed_at === null &&
    response.families.length === 0;
  const isSnapshotEmpty =
    response !== null &&
    response.computed_at !== null &&
    response.families.length === 0;
  const sortedFamilies = response ? sortFamiliesByStrength(response.families) : [];

  return (
    <main className="app-shell">
      <AppNav />

      <div className="constrained-content">
        <section className="openings-shell">
          <header className="openings-shell__hero">
            <div className="openings-shell__copy">
              <p className="openings-shell__eyebrow">Opening Scoreboard</p>
              <div className="openings-shell__title-row">
                <h1 className="openings-shell__title">Opening Families</h1>
                <span className="openings-shell__badge">
                  {selectedColorLabel} repertoire
                </span>
              </div>
              <p className="openings-shell__hint">
                Score is the 0-100 health read, confidence shows evidence
                strength, and coverage shows how much of the family tree you
                have actually touched.
              </p>
            </div>

            <aside className="openings-shell__callout">
              <p className="openings-shell__callout-label">Games</p>
              <p className="openings-shell__callout-text">
                Summed root evidence, so one game can contribute to more than
                one root.
              </p>
            </aside>
          </header>

          <div className="openings-shell__toolbar">
            <div
              className="openings-color-picker"
              role="group"
              aria-label="Opening color"
            >
              {COLOR_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={`openings-color-picker__button${playerColor === option.value ? " openings-color-picker__button--active" : ""}`}
                  aria-pressed={playerColor === option.value}
                  onClick={() => {
                    if (playerColor === option.value) {
                      return;
                    }

                    invalidatePendingRequests();
                    setLoading(true);
                    setError(null);
                    setResponse(null);
                    setPlayerColor(option.value);
                  }}
                >
                  {option.label}
                </button>
              ))}
            </div>

            <p className="openings-shell__toolbar-note">
              Showing the strongest openings first.
            </p>
          </div>

          {loading && (
            <section
              className="openings-state openings-state--loading"
              aria-live="polite"
              aria-label="Loading opening families"
            >
              <p className="openings-state__title">Loading opening families...</p>
              <div className="openings-grid openings-grid--loading">
                {Array.from({ length: LOADING_CARD_COUNT }).map((_, index) => (
                  <article
                    key={index}
                    className="opening-family-card opening-family-card--skeleton"
                    aria-hidden="true"
                  >
                    <div className="opening-family-card__skeleton-bar opening-family-card__skeleton-bar--short" />
                    <div className="opening-family-card__skeleton-bar opening-family-card__skeleton-bar--title" />
                    <div className="opening-family-card__skeleton-bar opening-family-card__skeleton-bar--long" />
                    <div className="opening-family-card__metrics opening-family-card__metrics--skeleton">
                      {Array.from({ length: 4 }).map((__, metricIndex) => (
                        <div
                          key={metricIndex}
                          className="opening-family-card__metric opening-family-card__metric--skeleton"
                        />
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            </section>
          )}

          {!loading && error && (
            <section className="openings-state openings-state--error" role="alert">
              <p className="openings-state__title">{error}</p>
              <p className="openings-state__body">
                The {selectedColorLabel.toLowerCase()} family snapshot did not
                load. Retry to fetch the latest cached scores.
              </p>
              <button
                className="chess-button primary"
                type="button"
                onClick={() => {
                  invalidatePendingRequests();
                  setLoading(true);
                  setError(null);
                  setResponse(null);
                  setRetryCount((value) => value + 1);
                }}
              >
                Retry
              </button>
            </section>
          )}

          {!loading && !error && isTrueNoEvidence && (
            <section className="openings-state openings-state--empty">
              <p className="openings-state__title">
                No opening evidence for {selectedColorLabel} yet.
              </p>
              <p className="openings-state__body">
                Play a few games with this color to start building opening
                stats.
              </p>
            </section>
          )}

          {!loading && !error && isSnapshotEmpty && (
            <section className="openings-state openings-state--empty">
              <p className="openings-state__title">
                No scored opening families are available for {selectedColorLabel}{" "}
                yet.
              </p>
              <p className="openings-state__body">
                A snapshot exists, but there are no family rows to show right
                now.
              </p>
            </section>
          )}

          {!loading && !error && hasFamilies && response && (
            <section
              className="openings-grid"
              aria-label={`${selectedColorLabel} opening families`}
            >
              {sortedFamilies.map((family) => {
                const tone = getPriorityTone(family.weakest_root_score);

                return (
                  <article
                    key={family.family_name}
                    className={`opening-family-card opening-family-card--${tone}`}
                  >
                    <div className="opening-family-card__topline">
                      <span className="opening-family-card__kicker">
                        {formatRootCount(family.root_count)}
                      </span>
                      <span className="opening-family-card__status">
                        {getPriorityLabel(family.weakest_root_score)}
                      </span>
                    </div>

                    <div className="opening-family-card__headline">
                      <h2 className="opening-family-card__title">
                        {family.family_name}
                      </h2>
                      <p className="opening-family-card__hint">
                        Weakest root:{" "}
                        <strong>{family.weakest_root_name}</strong>
                      </p>
                    </div>

                    <dl className="opening-family-card__metrics">
                      <div className="opening-family-card__metric">
                        <dt>Score</dt>
                        <dd>{formatScore(family.family_score)}</dd>
                      </div>
                      <div className="opening-family-card__metric">
                        <dt>Confidence</dt>
                        <dd>{formatPercent(family.family_confidence)}</dd>
                      </div>
                      <div className="opening-family-card__metric">
                        <dt>Coverage</dt>
                        <dd>{formatPercent(family.family_coverage)}</dd>
                      </div>
                      <div className="opening-family-card__metric">
                        <dt>Games</dt>
                        <dd>{formatGames(family.root_sample_size_sum)}</dd>
                      </div>
                    </dl>
                  </article>
                );
              })}
            </section>
          )}
        </section>
      </div>
    </main>
  );
}

export default OpeningsPage;
