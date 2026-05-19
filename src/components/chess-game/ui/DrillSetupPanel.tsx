import React, { memo } from "react";
import type { OpeningRootItem } from "../../../utils/api";
import OpponentAvatar from "./OpponentAvatar";

type DrillSetupPanelProps = {
  // Data
  openingFamilies: Array<{ family_name: string; roots: OpeningRootItem[] }> | null;
  selectedOpening: OpeningRootItem | null;
  playerColor: "white" | "black" | "random";
  engineElo: number;
  strictnessCp: number;
  maiaEloBins: readonly number[];
  botLabel: string;
  winDelta: number;
  lossDelta: number;

  // State
  isLoadingOpenings: boolean;
  isStarting: boolean;
  startError: string | null;

  // Handlers
  onSelectOpening: (opening: OpeningRootItem | null) => void;
  onPlayerColorChange: (color: "white" | "black" | "random") => void;
  onEngineEloChange: (elo: number) => void;
  onStrictnessChange: (cp: number) => void;
  onStartDrill: () => void;
};

export function strictnessFromCp(cp: number): "strict" | "standard" | "lenient" {
  if (cp <= 15) return "strict";
  if (cp <= 35) return "standard";
  return "lenient";
}

function labelForStrictnessCp(cp: number): string {
  const tier = strictnessFromCp(cp);
  if (tier === "strict") return `Strict — ${cp} cp loss allowed (perfect)`;
  if (tier === "standard") return `Standard — ~${cp} cp loss allowed`;
  return `Lenient — ${cp} cp loss allowed`;
}

const DrillSetupPanel = ({
  openingFamilies,
  selectedOpening,
  playerColor,
  engineElo,
  strictnessCp,
  maiaEloBins,
  botLabel,
  winDelta,
  lossDelta,
  isLoadingOpenings,
  isStarting,
  startError,
  onSelectOpening,
  onPlayerColorChange,
  onEngineEloChange,
  onStrictnessChange,
  onStartDrill,
}: DrillSetupPanelProps) => {
  const handleOpeningChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const value = e.target.value;
    if (!value || !openingFamilies) {
      onSelectOpening(null);
      return;
    }
    const match = openingFamilies
      .flatMap((f) => f.roots)
      .find((r) => r.opening_key === value);
    onSelectOpening(match ?? null);
  };

  return (
    <>
      <p className="chess-start-title">Opening</p>
      <select
        className="opening-selector"
        value={selectedOpening?.opening_key ?? ""}
        onChange={handleOpeningChange}
        disabled={isLoadingOpenings || isStarting}
      >
        {isLoadingOpenings || !openingFamilies ? (
          <option value="">{isLoadingOpenings ? "Loading openings..." : "Failed to load openings"}</option>
        ) : (
          <>
            <option value="">Select an opening...</option>
            {openingFamilies.map((family) => (
              <optgroup key={family.family_name} label={family.family_name}>
                {family.roots.map((root) => (
                  <option key={root.opening_key} value={root.opening_key}>
                    {root.eco ? `${root.eco} — ` : ""}
                    {root.opening_name}
                  </option>
                ))}
              </optgroup>
            ))}
          </>
        )}
      </select>

      <p className="chess-start-title">Side</p>
      <div className="chess-start-options">
        <button
          className={`chess-button toggle${playerColor === "white" ? " active" : ""}`}
          type="button"
          onClick={() => onPlayerColorChange("white")}
          disabled={isStarting}
        >
          White
        </button>
        <button
          className={`chess-button toggle${playerColor === "random" ? " active" : ""}`}
          type="button"
          onClick={() => onPlayerColorChange("random")}
          disabled={isStarting}
        >
          Random
        </button>
        <button
          className={`chess-button toggle${playerColor === "black" ? " active" : ""}`}
          type="button"
          onClick={() => onPlayerColorChange("black")}
          disabled={isStarting}
        >
          Black
        </button>
      </div>

      <p className="chess-start-title">Engine Difficulty</p>
      <div className="chess-elo-selector">
        <div className="chess-elo-slider-row">
          <input
            type="range"
            min={0}
            max={maiaEloBins.length - 1}
            step={1}
            value={maiaEloBins.indexOf(engineElo)}
            onChange={(e) => {
              const nextElo = maiaEloBins[Number(e.target.value)];
              if (nextElo !== undefined) {
                onEngineEloChange(nextElo);
              }
            }}
            disabled={isStarting}
            className="chess-elo-slider"
          />
        </div>
        <div className="chess-elo-bot-row">
          <OpponentAvatar mode="engine" engineElo={engineElo} size={70} />
          <span className="chess-elo-label">{botLabel}</span>
        </div>
      </div>
      <p className="elo-stakes">
        <span className="elo-stakes__win">Win +{winDelta}</span>
        {" / "}
        <span className="elo-stakes__loss">Loss {lossDelta}</span>
      </p>

      <p className="chess-start-title">Strictness</p>
      <div className="strictness-slider-row">
        <input
          type="range"
          min={0}
          max={50}
          step={1}
          value={strictnessCp}
          onChange={(e) => onStrictnessChange(Number(e.target.value))}
          disabled={isStarting}
          className="chess-elo-slider"
        />
      </div>
      <span className="strictness-label">{labelForStrictnessCp(strictnessCp)}</span>
      <span className="strictness-hint">Adjust tolerance for acceptable moves</span>

      <button
        className="chess-button primary"
        type="button"
        onClick={onStartDrill}
        disabled={isStarting || !selectedOpening}
      >
        {isStarting ? "Starting..." : "Start Drill"}
      </button>

      {startError && <p className="chess-start-error">{startError}</p>}
    </>
  );
};

export default memo(DrillSetupPanel);
