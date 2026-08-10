import { memo } from "react";
import { defaultPieces } from "react-chessboard";
import type { OpeningRootItem } from "../../../utils/api";
import OpeningPicker, { type OpeningPickerSelection } from "./OpeningPicker";
import {
  STRICTNESS_TIERS,
  bandForCp,
  strictnessFromCp,
  strictnessStopCopy,
} from "./DrillSetupPanel.helpers";

const WhiteKing = defaultPieces.wK;
const BlackKing = defaultPieces.bK;

type DrillSetupPanelProps = {
  // Data
  openingFamilies: Array<{ family_name: string; roots: OpeningRootItem[] }> | null;
  selectedOpening: OpeningRootItem | null;
  selectedLine: string[] | null;
  playerColor: "white" | "black";
  // null = no tier chosen yet; Start stays disabled until the user picks one.
  strictnessCp: number | null;

  // State
  isLoadingOpenings: boolean;
  isStarting: boolean;
  startError: string | null;

  // Handlers
  onSelectOpening: (selection: OpeningPickerSelection) => void;
  onPlayerColorChange: (color: "white" | "black") => void;
  onStrictnessChange: (cp: number) => void;
  onStartDrill: () => void;
};

const DrillSetupPanel = ({
  openingFamilies,
  selectedOpening,
  selectedLine,
  playerColor,
  strictnessCp,
  isLoadingOpenings,
  isStarting,
  startError,
  onSelectOpening,
  onPlayerColorChange,
  onStrictnessChange,
  onStartDrill,
}: DrillSetupPanelProps) => {
  return (
    <>
      {/* Only the fields scroll (g-drill-cta-clip). The Start action lives
          outside this box so a tight viewport never hides the CTA. */}
      <div className="drill-setup__fields">
        <div className="drill-field">
          <span className="drill-field__label">Opening</span>
          <div className="drill-field__control">
            <OpeningPicker
              openingFamilies={openingFamilies}
              selectedOpening={selectedOpening}
              selectedLine={selectedLine}
              playerColor={playerColor}
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
          <span className="drill-field__label">Strictness</span>
          <div className="drill-field__control">
            <div
              className="strictness-tier-grid"
              role="group"
              aria-label="Strictness"
              aria-describedby="strictness-stop-copy"
            >
              {STRICTNESS_TIERS.map((tier) => {
                const isActive =
                  strictnessCp != null && strictnessFromCp(strictnessCp) === tier.tier;
                return (
                  <button
                    key={tier.tier}
                    className={`strictness-tier-button${isActive ? " strictness-tier-button--active" : ""}`}
                    type="button"
                    title={tier.blurb}
                    aria-pressed={isActive}
                    onClick={() => onStrictnessChange(tier.seedCp)}
                    disabled={isStarting}
                  >
                    {tier.label}
                  </button>
                );
              })}
            </div>
            <span className="strictness-label" id="strictness-stop-copy">
              {strictnessCp == null
                ? "Pick a strictness to start"
                : strictnessStopCopy(strictnessCp)}
            </span>
            {strictnessCp != null && (
              <div className="strictness-slider-row">
                <input
                  type="range"
                  min={bandForCp(strictnessCp).min}
                  max={bandForCp(strictnessCp).max}
                  step={1}
                  value={strictnessCp}
                  aria-label="Fine-tune strictness"
                  aria-describedby="strictness-stop-copy"
                  onChange={(e) => onStrictnessChange(Number(e.target.value))}
                  disabled={isStarting}
                  className="chess-elo-slider"
                />
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="drill-setup__actions">
        <button
          className="chess-button primary"
          type="button"
          onClick={onStartDrill}
          disabled={isStarting || !selectedOpening || strictnessCp == null}
        >
          {isStarting ? "Starting..." : "Start Drill"}
        </button>

        {startError && <p className="chess-start-error">{startError}</p>}
      </div>
    </>
  );
};

export default memo(DrillSetupPanel);
