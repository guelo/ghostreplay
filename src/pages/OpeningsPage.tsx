import { useCallback, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { defaultPieces } from "react-chessboard";
import AppNav from "../components/AppNav";
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

const WhiteKing = defaultPieces.wK;
const BlackKing = defaultPieces.bK;

const COLOR_OPTIONS: Array<{
  label: string;
  value: OpeningPlayerColor;
  King: typeof WhiteKing;
}> = [
  { label: "White", value: "white", King: WhiteKing },
  { label: "Black", value: "black", King: BlackKing },
];

/** Route and page chrome for the shared interactive openings-tree explorer. */
function OpeningsPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { playerColor, moves, opening } =
    parseOpeningsSearchParams(searchParams);

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

  const switchColor = (color: OpeningPlayerColor) => {
    if (color === playerColor) return;
    setSearchParams(buildOpeningsSearchParams({ playerColor: color, moves }));
  };

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
    <main className="app-shell openings-page">
      <AppNav />

      <div className="constrained-content">
        <section className="openings-tree">
          <header className="openings-tree__header">
            <h1 className="openings-tree__title">Openings Tree</h1>
            <div className="openings-tree__controls-row">
              <div
                className="openings-color-picker"
                role="group"
                aria-label="Playing as"
              >
                <span className="openings-tree__color-label">Playing as:</span>
                <div className="mode-toggle-row segmented-toggle openings-color-picker__toggle">
                  {COLOR_OPTIONS.map(({ label, value, King }) => (
                    <button
                      key={value}
                      type="button"
                      className={`chess-button toggle${
                        playerColor === value ? " active" : ""
                      }`}
                      aria-pressed={playerColor === value}
                      onClick={() => switchColor(value)}
                    >
                      <span className="openings-color-picker__piece">
                        <King />
                      </span>
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <OpeningsMetricsLegend />
            </div>
          </header>

          <OpeningsTreeExplorer
            route={{ playerColor, moves, opening }}
            onSelectLine={selectLine}
            onCanonicalLine={canonicalizeLine}
            expandedAction={expandedAction}
          />
        </section>
      </div>
    </main>
  );
}

export default OpeningsPage;
