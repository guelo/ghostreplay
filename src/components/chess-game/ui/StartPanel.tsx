import { memo, useState } from "react";
import { defaultPieces } from "react-chessboard";
import type { DrillSelection } from "../../../openings/drillSelection";
import type { OpeningRootItem } from "../../../utils/api";
import { MAIA_BOT_NAMES } from "../config";
import { eloStakes } from "../elo";
import OpponentAvatar from "./OpponentAvatar";
import DrillSetupPanel from "./DrillSetupPanel";

const WhiteKing = defaultPieces.wK;
const BlackKing = defaultPieces.bK;

type StartPlaySide = "white" | "random" | "black";

export type StartDrillDraft = {
  engineElo: number;
  strictnessCp: number;
  playerColor: "white" | "black";
  selection: DrillSelection;
};

type StartPanelProps = {
  isDrillMode: boolean;
  isStartingGame: boolean;
  startError: string | null;
  onClose: () => void;
  onSwitchToPlayMode: () => void;
  onSwitchToDrillMode: () => void;

  maiaEloBins: readonly number[];

  // Seeds — computed by ChessGame (prefill / again / nav / openingFamilies-match)
  // WITHOUT committing to the game store. The panel drafts from these and only
  // commits on Start, so opening/cancelling the popup never mutates game state.
  seedEngineElo: number;
  // Always null in practice (g-09mu force-always): every panel open starts with
  // no strictness tier selected so the user makes a conscious choice.
  seedStrictnessCp: number | null;
  seedColor: "white" | "black";
  seedSelection: DrillSelection | null;

  // For locally derived win/loss stakes.
  playerRating: number;
  isProvisional: boolean;

  // Drill data
  openingFamilies: Array<{ family_name: string; roots: OpeningRootItem[] }> | null;
  isLoadingOpenings: boolean;

  // Commit on exit
  onStartPlay: (side: StartPlaySide, engineElo: number) => void;
  onStartDrill: (draft: StartDrillDraft) => void;
};

const StartPanel = ({
  isDrillMode,
  isStartingGame,
  startError,
  onClose,
  onSwitchToPlayMode,
  onSwitchToDrillMode,
  maiaEloBins,
  seedEngineElo,
  seedStrictnessCp,
  seedColor,
  seedSelection,
  playerRating,
  isProvisional,
  openingFamilies,
  isLoadingOpenings,
  onStartPlay,
  onStartDrill,
}: StartPanelProps) => {
  // Live draft state. Slider/clicks mutate only these, so dragging re-renders
  // this subtree alone — never ChessGame or the sibling <Chessboard>.
  const [draftElo, setDraftElo] = useState(seedEngineElo);
  const [draftStrictnessCp, setDraftStrictnessCp] = useState(seedStrictnessCp);
  const [draftColor, setDraftColor] = useState(seedColor);
  const [draftSelection, setDraftSelection] = useState(seedSelection);

  // Resync a draft when its seed prop changes (async reseeds: prefill effect,
  // again-settings, openingFamilies-match). A live drag leaves the seed prop
  // untouched, so it never fights the resync. The whole selection resyncs together,
  // including line-only or mode-only changes with the same opening identity.
  const [previousSeeds, setPreviousSeeds] = useState(() => ({
    engineElo: seedEngineElo,
    strictnessCp: seedStrictnessCp,
    color: seedColor,
    selection: seedSelection,
  }));
  const eloChanged = previousSeeds.engineElo !== seedEngineElo;
  const strictnessChanged = previousSeeds.strictnessCp !== seedStrictnessCp;
  const colorChanged = previousSeeds.color !== seedColor;
  const selectionChanged = previousSeeds.selection !== seedSelection;
  if (eloChanged || strictnessChanged || colorChanged || selectionChanged) {
    setPreviousSeeds({
      engineElo: seedEngineElo,
      strictnessCp: seedStrictnessCp,
      color: seedColor,
      selection: seedSelection,
    });
  }
  if (eloChanged) {
    setDraftElo(seedEngineElo);
  }
  if (strictnessChanged) {
    setDraftStrictnessCp(seedStrictnessCp);
  }
  if (colorChanged) {
    setDraftColor(seedColor);
  }
  if (selectionChanged) {
    setDraftSelection(seedSelection);
  }

  const botLabel = MAIA_BOT_NAMES[draftElo as keyof typeof MAIA_BOT_NAMES];
  const { winDelta, lossDelta } = eloStakes(playerRating, draftElo, isProvisional);

  return (
    <div className={`chess-start-panel${isDrillMode ? " chess-start-panel--drill" : ""}`}>
      <button
        className="chess-start-close"
        type="button"
        onClick={onClose}
        disabled={isStartingGame}
        aria-label="Close"
      >
        ×
      </button>

      <div className="mode-toggle-row segmented-toggle">
        <button
          className={`chess-button toggle${!isDrillMode ? " active" : ""}`}
          type="button"
          onClick={onSwitchToPlayMode}
          disabled={isStartingGame}
        >
          Play
        </button>
        <button
          className={`chess-button toggle${isDrillMode ? " active" : ""}`}
          type="button"
          onClick={onSwitchToDrillMode}
          disabled={isStartingGame}
        >
          Drill
        </button>
      </div>

      {isDrillMode ? (
        <div className="chess-start-scroll chess-start-scroll--drill">
          <DrillSetupPanel
            openingFamilies={openingFamilies}
            selectedOpening={draftSelection?.opening ?? null}
            selectedLine={draftSelection?.line ?? null}
            routeMode={draftSelection?.routeMode ?? "auto"}
            playerColor={draftColor}
            strictnessCp={draftStrictnessCp}
            isLoadingOpenings={isLoadingOpenings}
            isStarting={isStartingGame}
            startError={startError}
            onSelectOpening={(selection) => {
              setDraftSelection({ ...selection, routeMode: "auto" });
            }}
            onPlayerColorChange={setDraftColor}
            onStrictnessChange={setDraftStrictnessCp}
            onStartDrill={() => {
              if (!draftSelection || draftStrictnessCp == null) return;
              onStartDrill({
                engineElo: draftElo,
                strictnessCp: draftStrictnessCp,
                playerColor: draftColor,
                selection: draftSelection,
              });
            }}
          />
        </div>
      ) : (
        <>
          <div className="chess-start-scroll">
            <p className="chess-start-title">Difficulty</p>
            <div className="chess-elo-selector">
              <div className="chess-elo-slider-row">
                <input
                  type="range"
                  min={0}
                  max={maiaEloBins.length - 1}
                  step={1}
                  value={maiaEloBins.indexOf(draftElo)}
                  onChange={(e) => {
                    const nextElo = maiaEloBins[Number(e.target.value)];
                    if (nextElo !== undefined) {
                      setDraftElo(nextElo);
                    }
                  }}
                  disabled={isStartingGame}
                  className="chess-elo-slider"
                />
              </div>
              <div className="chess-elo-bot-row">
                <OpponentAvatar mode="engine" engineElo={draftElo} size={70} />
                <span className="chess-elo-label">{botLabel}</span>
              </div>
            </div>
            <p className="elo-stakes">
              <span className="elo-stakes__win">Win +{winDelta}</span>
              {" / "}
              <span className="elo-stakes__loss">Loss {lossDelta}</span>
            </p>
            <p className="chess-start-title">Side</p>
          </div>

          <div className="chess-start-options">
            <button
              className="play-side-button"
              type="button"
              aria-label="Play White"
              onClick={() => onStartPlay("white", draftElo)}
              disabled={isStartingGame}
            >
              <span className="play-side-button__piece">
                <WhiteKing />
              </span>
              <span className="play-side-button__label">White</span>
            </button>
            <button
              className="play-side-button"
              type="button"
              aria-label="Play Random"
              onClick={() => onStartPlay("random", draftElo)}
              disabled={isStartingGame}
            >
              <span className="play-side-button__piece play-side-button__piece--split">
                <span className="play-side-king play-side-king--left">
                  <WhiteKing />
                </span>
                <span className="play-side-king play-side-king--right">
                  <BlackKing />
                </span>
              </span>
              <span className="play-side-button__label">Random</span>
            </button>
            <button
              className="play-side-button"
              type="button"
              aria-label="Play Black"
              onClick={() => onStartPlay("black", draftElo)}
              disabled={isStartingGame}
            >
              <span className="play-side-button__piece">
                <BlackKing />
              </span>
              <span className="play-side-button__label">Black</span>
            </button>
          </div>
          {startError && <p className="chess-start-error">{startError}</p>}
        </>
      )}
    </div>
  );
};

export default memo(StartPanel);
