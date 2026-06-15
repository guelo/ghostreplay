import { memo } from "react";
import { defaultPieces } from "react-chessboard";
import type { OpeningRootItem } from "../../../utils/api";
import OpponentAvatar from "./OpponentAvatar";
import OpeningPicker from "./OpeningPicker";
import { strictnessFromCp } from "./DrillSetupPanel.helpers";

const WhiteKing = defaultPieces.wK;
const BlackKing = defaultPieces.bK;

type DrillSetupPanelProps = {
  // Data
  openingFamilies: Array<{ family_name: string; roots: OpeningRootItem[] }> | null;
  selectedOpening: OpeningRootItem | null;
  playerColor: "white" | "black";
  engineElo: number;
  strictnessCp: number;
  maiaEloBins: readonly number[];
  botLabel: string;

  // State
  isLoadingOpenings: boolean;
  isStarting: boolean;
  startError: string | null;

  // Handlers
  onSelectOpening: (opening: OpeningRootItem | null) => void;
  onPlayerColorChange: (color: "white" | "black") => void;
  onEngineEloChange: (elo: number) => void;
  onStrictnessChange: (cp: number) => void;
  onStartDrill: () => void;
};

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
  isLoadingOpenings,
  isStarting,
  startError,
  onSelectOpening,
  onPlayerColorChange,
  onEngineEloChange,
  onStrictnessChange,
  onStartDrill,
}: DrillSetupPanelProps) => {
  return (
    <>
      <div className="drill-field">
        <span className="drill-field__label">Opening</span>
        <div className="drill-field__control">
          <OpeningPicker
            openingFamilies={openingFamilies}
            selectedOpening={selectedOpening}
            disabled={isStarting}
            isLoading={isLoadingOpenings}
            onSelect={onSelectOpening}
          />
        </div>
      </div>

      <div className="drill-field">
        <span className="drill-field__label">Side</span>
        <div className="drill-field__control chess-start-options">
          <button
            className={`play-side-button${playerColor === "white" ? " play-side-button--active" : ""}`}
            type="button"
            onClick={() => onPlayerColorChange("white")}
            disabled={isStarting}
          >
            <span className="play-side-button__piece">
              <WhiteKing />
            </span>
            <span className="play-side-button__label">White</span>
          </button>
          <button
            className={`play-side-button${playerColor === "black" ? " play-side-button--active" : ""}`}
            type="button"
            onClick={() => onPlayerColorChange("black")}
            disabled={isStarting}
          >
            <span className="play-side-button__piece">
              <BlackKing />
            </span>
            <span className="play-side-button__label">Black</span>
          </button>
        </div>
      </div>

      <div className="drill-field">
        <span className="drill-field__label">Engine Difficulty</span>
        <div className="drill-field__control chess-elo-selector">
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
            <OpponentAvatar mode="engine" engineElo={engineElo} size={48} />
            <span className="chess-elo-label">{botLabel}</span>
          </div>
        </div>
      </div>

      <div className="drill-field">
        <span className="drill-field__label">Strictness</span>
        <div className="drill-field__control">
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
        </div>
      </div>

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
