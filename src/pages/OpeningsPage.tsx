import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Chessboard } from "react-chessboard";
import AppNav from "../components/AppNav";
import { getOpeningBook } from "../openings/openingBook";
import {
  getOpeningChildren,
  type ChildrenResponse,
  type OpeningPlayerColor,
} from "../utils/api";
import "../App.css";

const COLOR_OPTIONS: Array<{ label: string; value: OpeningPlayerColor }> = [
  { label: "White", value: "white" },
  { label: "Black", value: "black" },
];

const LOADING_CARD_COUNT = 3;

type OpeningRoute = {
  playerColor: OpeningPlayerColor;
  openingKey?: string;
  path?: string[];
};

function buildOpeningsSearchParams(route: OpeningRoute): URLSearchParams {
  const params = new URLSearchParams({
    color: route.playerColor,
  });

  if (route.openingKey) {
    params.set("opening", route.openingKey);

    for (const pathKey of route.path ?? []) {
      params.append("path", pathKey);
    }
  }

  return params;
}

function getColorLabel(playerColor: OpeningPlayerColor): string {
  return playerColor === "white" ? "White" : "Black";
}

function formatScore(value: number | null): string {
  if (value === null) {
    return "—";
  }

  return String(Math.round(value));
}

function normalizePercentValue(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }

  const normalizedValue = value <= 1 ? value * 100 : value;
  return Math.min(100, Math.max(0, normalizedValue));
}

function formatPercent(value: number | null): string {
  if (value === null) {
    return "—";
  }

  return `${Math.round(normalizePercentValue(value))}%`;
}

function formatGames(value: number | null): string {
  if (value === null) {
    return "—";
  }

  return value.toLocaleString();
}

function getPriorityTone(
  score: number | null,
): "alert" | "watch" | "steady" | "muted" {
  if (score === null) {
    return "muted";
  }

  if (score < 45) {
    return "alert";
  }

  if (score < 65) {
    return "watch";
  }

  return "steady";
}

function getPriorityLabel(score: number | null): string {
  if (score === null) {
    return "No Data";
  }

  if (score >= 85) {
    return "A";
  }

  if (score >= 70) {
    return "B";
  }

  if (score >= 55) {
    return "C";
  }

  if (score >= 45) {
    return "D";
  }

  return "F";
}

function formatChildCount(childCount: number): string {
  if (childCount === 0) {
    return "No children";
  }

  return `${childCount} ${childCount === 1 ? "child" : "children"}`;
}

function formatOpeningMoveLine(pgn: string): string {
  return pgn.replace(/(\d+)\.\s+/g, "$1.");
}

function getOpeningMoveLine(
  openingKey: string,
  moveLinesByFen: Map<string, string> | null,
): string | null {
  if (!moveLinesByFen) {
    return null;
  }

  const line = moveLinesByFen.get(openingKey);
  return line ? formatOpeningMoveLine(line) : null;
}

function sortChildrenByStrength(
  children: ChildrenResponse["children"],
): ChildrenResponse["children"] {
  return [...children].sort((left, right) => {
    if (left.subtree_score === null && right.subtree_score !== null) {
      return 1;
    }

    if (left.subtree_score !== null && right.subtree_score === null) {
      return -1;
    }

    if (left.subtree_score !== null && right.subtree_score !== null) {
      if (left.subtree_score !== right.subtree_score) {
        return right.subtree_score - left.subtree_score;
      }
    }

    if (left.weakest_root_score !== null && right.weakest_root_score !== null) {
      if (left.weakest_root_score !== right.weakest_root_score) {
        return right.weakest_root_score - left.weakest_root_score;
      }
    }

    return left.opening_name.localeCompare(right.opening_name);
  });
}

function OpeningsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [response, setResponse] = useState<ChildrenResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const [moveLinesByFen, setMoveLinesByFen] = useState<Map<
    string,
    string
  > | null>(null);
  const requestVersionRef = useRef(0);

  const rawColor = searchParams.get("color");
  const playerColor: OpeningPlayerColor =
    rawColor === "black" ? "black" : "white";
  const requestedOpeningKey = searchParams.get("opening");
  const rawPath = searchParams.getAll("path");
  const requestedPath = requestedOpeningKey ? rawPath : [];
  const requestedRoute = buildOpeningsSearchParams({
    playerColor,
    openingKey: requestedOpeningKey ?? undefined,
    path: requestedPath,
  });
  const requestedRouteString = requestedRoute.toString();
  const requestedPathKey = JSON.stringify(requestedPath);
  const needsInitialCanonicalization =
    rawColor !== playerColor || (!requestedOpeningKey && rawPath.length > 0);

  const invalidatePendingRequests = () => {
    requestVersionRef.current += 1;
  };

  const navigateToRoute = (route: OpeningRoute, replace = false) => {
    invalidatePendingRequests();
    setLoading(true);
    setError(null);
    setResponse(null);
    setSearchParams(buildOpeningsSearchParams(route), { replace });
  };

  useEffect(() => {
    if (!needsInitialCanonicalization) {
      return;
    }

    setSearchParams(
      buildOpeningsSearchParams({
        playerColor,
        openingKey: requestedOpeningKey ?? undefined,
        path: requestedPath,
      }),
      { replace: true },
    );
  }, [
    needsInitialCanonicalization,
    playerColor,
    requestedOpeningKey,
    requestedPathKey,
    setSearchParams,
  ]);

  useEffect(() => {
    let active = true;

    getOpeningBook()
      .then((book) => {
        if (!active) {
          return;
        }

        setMoveLinesByFen(
          new Map(book.entries.map((entry) => [entry.epd, entry.pgn])),
        );
      })
      .catch(() => {
        if (!active) {
          return;
        }

        setMoveLinesByFen(new Map());
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    const requestVersion = requestVersionRef.current + 1;
    requestVersionRef.current = requestVersion;

    setLoading(true);
    setError(null);
    setResponse(null);

    getOpeningChildren({
      playerColor,
      parentKey: requestedOpeningKey ?? undefined,
      path: requestedPath,
    })
      .then((data) => {
        if (!active || requestVersionRef.current !== requestVersion) {
          return;
        }

        const canonicalRoute = buildOpeningsSearchParams({
          playerColor,
          openingKey: data.canonical_opening_key ?? undefined,
          path: data.canonical_path,
        });
        if (canonicalRoute.toString() !== requestedRouteString) {
          setSearchParams(canonicalRoute, { replace: true });
        }

        setResponse(data);
      })
      .catch((err: unknown) => {
        if (!active || requestVersionRef.current !== requestVersion) {
          return;
        }

        setResponse(null);
        setError(
          err instanceof Error ? err.message : "Failed to load openings",
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
  }, [
    playerColor,
    requestedOpeningKey,
    requestedPathKey,
    requestedRouteString,
    retryCount,
    setSearchParams,
  ]);

  const selectedColorLabel = getColorLabel(playerColor);
  const breadcrumbs = response?.breadcrumbs ?? [];
  const fullBreadcrumbs = breadcrumbs.length
    ? [
        {
          opening_key: "__start__",
          opening_name: "Start",
          is_current: false,
        },
        ...breadcrumbs,
      ]
    : [];
  const currentBreadcrumb = breadcrumbs.at(-1) ?? null;
  const parentBreadcrumb =
    breadcrumbs.length > 1 ? (breadcrumbs.at(-2) ?? null) : null;
  const currentTitle = currentBreadcrumb?.opening_name ?? "OPENING SCOREBOARD";
  const currentBranchStats = response?.current_branch_stats ?? null;
  const heroTone = getPriorityTone(currentBranchStats?.score ?? null);
  const heroStatsLabel = currentBreadcrumb ? "Current branch" : "Repertoire-wide";
  const heroStatsCaption = currentBreadcrumb
    ? "Selected opening aggregate"
    : "All scored roots in this repertoire";
  const parentMoveLine = parentBreadcrumb
    ? getOpeningMoveLine(parentBreadcrumb.opening_key, moveLinesByFen)
    : null;
  const hasScoredCurrentBranch =
    response !== null && response.current_branch_stats.root_count > 0;
  const isTrueNoEvidence =
    response !== null && response.computed_at === null && !hasScoredCurrentBranch;
  const isSnapshotEmpty =
    response !== null && response.computed_at !== null && !hasScoredCurrentBranch;
  const showChildrenGrid =
    response !== null && response.children.length > 0 && hasScoredCurrentBranch;
  const isStructuralLeaf =
    response !== null &&
    response.children.length === 0 &&
    response.canonical_opening_key !== null &&
    hasScoredCurrentBranch;
  const sortedChildren = response
    ? sortChildrenByStrength(response.children)
    : [];

  return (
    <main className="app-shell">
      <AppNav />

      <div className="constrained-content">
        <section className="openings-shell">
          <header className="openings-shell__hero">
            <div className="openings-shell__copy">
              <div className="openings-shell__title-row">
                <h1 className="openings-shell__title">{currentTitle}</h1>
                <span className="openings-shell__badge">
                  {selectedColorLabel} repertoire
                </span>
              </div>
              {fullBreadcrumbs.length > 0 && (
                <nav
                  className="openings-shell__breadcrumbs"
                  aria-label="Opening breadcrumbs"
                >
                  {fullBreadcrumbs.map((breadcrumb, index) => {
                    if (breadcrumb.opening_key === "__start__") {
                      return (
                        <button
                          key={breadcrumb.opening_key}
                          type="button"
                          className="openings-shell__crumb"
                          onClick={() => {
                            navigateToRoute({
                              playerColor,
                            });
                          }}
                        >
                          {breadcrumb.opening_name}
                        </button>
                      );
                    }

                    if (breadcrumb.is_current) {
                      return (
                        <span
                          key={breadcrumb.opening_key}
                          className="openings-shell__crumb openings-shell__crumb--current"
                        >
                          {breadcrumb.opening_name}
                        </span>
                      );
                    }

                    return (
                      <button
                        key={breadcrumb.opening_key}
                        type="button"
                        className="openings-shell__crumb"
                        onClick={() => {
                          navigateToRoute({
                            playerColor,
                            openingKey: breadcrumb.opening_key,
                            path: fullBreadcrumbs
                              .slice(1, index)
                              .map((item) => item.opening_key),
                          });
                        }}
                      >
                        {breadcrumb.opening_name}
                      </button>
                    );
                  })}
                </nav>
              )}
              <p className="openings-shell__hint">
                Score is your 0-100 result in this opening branch. Confidence
                shows how solid the sample is, and coverage shows how much of
                that branch you have actually played.
              </p>
            </div>

            <div className="openings-shell__hero-rail">
              <aside
                className={`openings-shell__stats-card openings-shell__stats-card--${heroTone}`}
                aria-label="Current branch stats"
              >
                <div className="openings-shell__stats-copy">
                  <p className="openings-shell__stats-label">{heroStatsLabel}</p>
                  <p className="openings-shell__stats-caption">
                    {heroStatsCaption}
                  </p>
                </div>
                <dl className="openings-shell__stats-grid">
                  <div className="openings-shell__stats-metric">
                    <dt>Score</dt>
                    <dd>{formatScore(currentBranchStats?.score ?? null)}</dd>
                  </div>
                  <div className="openings-shell__stats-metric">
                    <dt>Coverage</dt>
                    <dd>{formatPercent(currentBranchStats?.coverage ?? null)}</dd>
                  </div>
                  <div className="openings-shell__stats-metric">
                    <dt>Games</dt>
                    <dd>{formatGames(currentBranchStats?.sample_size ?? null)}</dd>
                  </div>
                  <div className="openings-shell__stats-metric">
                    <dt>Confidence</dt>
                    <dd>
                      {formatPercent(currentBranchStats?.confidence ?? null)}
                    </dd>
                  </div>
                </dl>
              </aside>

              {parentBreadcrumb && (
                <aside className="openings-shell__parent-card">
                  <p className="openings-shell__parent-label">Parent branch</p>
                  <button
                    type="button"
                    className="openings-shell__parent-button"
                    onClick={() => {
                      navigateToRoute({
                        playerColor,
                        openingKey: parentBreadcrumb.opening_key,
                        path: breadcrumbs
                          .slice(0, -2)
                          .map((item) => item.opening_key),
                      });
                    }}
                  >
                    <span className="openings-shell__parent-name">
                      {parentBreadcrumb.opening_name}
                    </span>
                    <span className="openings-shell__parent-meta">
                      {parentMoveLine ?? "Line unavailable."}
                    </span>
                  </button>
                </aside>
              )}
            </div>
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

                    navigateToRoute({
                      playerColor: option.value,
                    });
                  }}
                >
                  {option.label}
                </button>
              ))}
            </div>

            <p className="openings-shell__toolbar-note">
              Showing the strongest branches first, with unscored branches at
              the end.
            </p>
          </div>

          {loading && (
            <section
              className="openings-state openings-state--loading"
              aria-live="polite"
              aria-label="Loading openings"
            >
              <p className="openings-state__title">Loading openings...</p>
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
            <section
              className="openings-state openings-state--error"
              role="alert"
            >
              <p className="openings-state__title">{error}</p>
              <p className="openings-state__body">
                The {selectedColorLabel.toLowerCase()} openings snapshot did not
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
                No scored openings are available for {selectedColorLabel} yet.
              </p>
              <p className="openings-state__body">
                A snapshot exists, but this branch has no scored roots right
                now.
              </p>
            </section>
          )}

          {!loading && !error && isStructuralLeaf && (
            <section className="openings-state openings-state--empty">
              <p className="openings-state__title">
                No deeper named openings under {response?.parent_name}.
              </p>
              <p className="openings-state__body">
                This branch is a structural leaf in the named opening tree.
              </p>
            </section>
          )}

          {!loading && !error && showChildrenGrid && response && (
            <section
              className="openings-grid"
              aria-label={`${selectedColorLabel} openings`}
            >
              {sortedChildren.map((child) => {
                const tone = getPriorityTone(child.subtree_score);
                const isUnscored = child.subtree_root_count === 0;
                const canDrillDown = child.child_count > 0;
                const openingMoveLine = getOpeningMoveLine(
                  child.opening_key,
                  moveLinesByFen,
                );
                const statusLabel = getPriorityLabel(child.subtree_score);
                const cardClassName = `opening-family-card opening-family-card--${tone}${canDrillDown ? " opening-family-card--interactive" : ""}`;
                const headline = (
                  <>
                    <div className="opening-family-card__headline">
                      <h2 className="opening-family-card__title">
                        {child.opening_name}
                      </h2>
                      <p className="opening-family-card__hint">
                        Moves:{" "}
                        <strong>
                          {openingMoveLine ?? "Line unavailable."}
                        </strong>
                      </p>
                      {isUnscored && (
                        <p className="opening-family-card__subhint">
                          No scored roots in this subtree yet.
                        </p>
                      )}
                    </div>

                    <div className="opening-family-card__overview">
                      <div
                        className="opening-family-card__board"
                        aria-hidden="true"
                      >
                        <Chessboard
                          options={{
                            position: child.opening_key,
                            boardOrientation: playerColor,
                            allowDragging: false,
                            animationDurationInMs: 0,
                            boardStyle: {
                              borderRadius: "10px",
                              pointerEvents: "none",
                            },
                          }}
                        />
                      </div>
                      <dl className="opening-family-card__score-panel">
                        <div className="opening-family-card__score-metric">
                          <dt>Score</dt>
                          <dd>{formatScore(child.subtree_score)}</dd>
                        </div>
                        <div
                          aria-label={`Status ${statusLabel}`}
                          className="opening-family-card__grade"
                        >
                          {statusLabel}
                        </div>
                      </dl>
                    </div>

                    <dl className="opening-family-card__metrics">
                      <div className="opening-family-card__metric">
                        <dt>Coverage</dt>
                        <dd>{formatPercent(child.subtree_coverage)}</dd>
                      </div>
                      <div className="opening-family-card__metric">
                        <dt>Games</dt>
                        <dd>{formatGames(child.subtree_sample_size)}</dd>
                      </div>
                      <div className="opening-family-card__metric">
                        <dt>Confidence</dt>
                        <dd>{formatPercent(child.subtree_confidence)}</dd>
                      </div>
                    </dl>

                    <div className="opening-family-card__footer">
                      <span className="opening-family-card__footer-note">
                        {formatChildCount(child.child_count)}
                      </span>
                      {canDrillDown && (
                        <span className="opening-family-card__drilldown">
                          Drill down
                        </span>
                      )}
                    </div>
                  </>
                );

                if (canDrillDown) {
                  return (
                    <button
                      key={child.opening_key}
                      type="button"
                      className={cardClassName}
                      onClick={() => {
                        navigateToRoute({
                          playerColor,
                          openingKey: child.opening_key,
                          path: response.canonical_opening_key
                            ? [
                                ...response.canonical_path,
                                response.canonical_opening_key,
                              ]
                            : [],
                        });
                      }}
                    >
                      {headline}
                    </button>
                  );
                }

                return (
                  <article key={child.opening_key} className={cardClassName}>
                    {headline}
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
