import { useCallback, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import AppNav from "../components/AppNav";
import OpeningSideToggle from "../components/OpeningSideToggle";
import OpeningsMetricsLegend from "../components/OpeningsMetricsLegend";
import OpeningsTreeExplorer, {
  type OpeningsTreeActionTarget,
} from "../components/OpeningsTreeExplorer";
import { captureEvent } from "../analytics/posthog";
import {
  buildCanonicalReplacement,
  buildOpeningsSearchParams,
  parseOpeningsSearchParams,
} from "../openings/route";
import type { OpeningPlayerColor } from "../utils/api";
import "../App.css";

function SelectedOpeningsExplorer({
  playerColor,
  moves,
  opening,
}: {
  playerColor: OpeningPlayerColor;
  moves: string[];
  opening: string | null;
}) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const selectLine = useCallback(
    (newLine: string[]) => {
      captureEvent("opening_explored", {
        source: "openings_page",
        from_key: moves.join(","),
        to_key: newLine.join(","),
        depth: newLine.length,
        player_color: playerColor,
      });
      setSearchParams(buildOpeningsSearchParams({ playerColor, moves: newLine }));
    },
    [moves, playerColor, setSearchParams],
  );

  const canonicalizeLine = useCallback(
    (canonicalLine: string[]) => {
      const replacement = buildCanonicalReplacement(
        searchParams,
        playerColor,
        canonicalLine,
      );
      if (replacement) {
        setSearchParams(replacement, { replace: true });
      }
    },
    [playerColor, searchParams, setSearchParams],
  );

  const expandedAction = useMemo(
    () => ({
      label: "Start Drill",
      onSelect: (target: OpeningsTreeActionTarget) => {
        navigate("/play", {
          state: { drillSetup: { ...target, playerColor } },
        });
      },
    }),
    [navigate, playerColor],
  );

  return (
    <OpeningsTreeExplorer
      route={{ playerColor, moves, opening }}
      onSelectLine={selectLine}
      onCanonicalLine={canonicalizeLine}
      expandedAction={expandedAction}
    />
  );
}

/** Route and page chrome for the shared interactive openings-tree explorer. */
function OpeningsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { playerColor, moves, opening } =
    parseOpeningsSearchParams(searchParams);

  const changeColor = (color: OpeningPlayerColor) => {
    if (color === playerColor) return;

    const nextParams = buildOpeningsSearchParams({
      playerColor: color,
      moves,
      opening: opening ?? undefined,
    });

    if (playerColor === null) {
      setSearchParams(nextParams, { replace: true });
      return;
    }

    setSearchParams(nextParams);
  };

  return (
    <main className="app-shell openings-page">
      <AppNav />

      <div className="constrained-content">
        <section className="openings-tree">
          <header className="openings-tree__header">
            <h1 className="openings-tree__title">Openings Tree</h1>
            <div className="openings-tree__controls-row">
              <div className="openings-color-picker">
                <span className="openings-tree__color-label">Playing as:</span>
                <OpeningSideToggle
                  playerColor={playerColor}
                  onPlayerColorChange={changeColor}
                />
              </div>
              <OpeningsMetricsLegend />
            </div>
          </header>

          {playerColor === null ? (
            <div className="openings-selection-gate">
              <section
                className="openings-state openings-selection-gate__card"
                aria-labelledby="openings-selection-gate-title"
              >
                <h2
                  id="openings-selection-gate-title"
                  className="openings-state__title"
                >
                  Choose a side to load your opening tree
                </h2>
                <p className="openings-state__body">
                  Select White or Black above to explore your repertoire.
                </p>
              </section>
            </div>
          ) : (
            <SelectedOpeningsExplorer
              playerColor={playerColor}
              moves={moves}
              opening={opening}
            />
          )}
        </section>
      </div>
    </main>
  );
}

export default OpeningsPage;
