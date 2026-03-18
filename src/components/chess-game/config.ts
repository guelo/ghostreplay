/** Maia3 ELO bins – must match backend/app/maia3_client.py:ELO_BINS */
export const MAIA_ELO_BINS = [
  600, 800, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000,
  2200, 2400, 2600,
] as const;

export const MAIA_BOT_NAMES: Record<(typeof MAIA_ELO_BINS)[number], string> = {
  600: "Boo Bud 600",
  800: "Wisp Cub 800",
  1000: "Phantom Puff 1000",
  1100: "Misty Paws 1100",
  1200: "Specter Scout 1200",
  1300: "Boo Bishop 1300",
  1400: "Wisp Gambit 1400",
  1500: "Phantom Tempo 1500",
  1600: "Misty Sharp 1600",
  1700: "Specter Prep 1700",
  1800: "Boo Tactician 1800",
  1900: "Wraith Endgame 1900",
  2000: "Ghost Master 2000",
  2200: "Phantom Engine 2200",
  2400: "Specter Legend 2400",
  2600: "Wraith Nova 2600",
};

export const STARTING_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

export const ANALYSIS_UPLOAD_TIMEOUT_MS = 6000;

export const BLUNDER_AUDIO_CLIPS = Array.from(
  { length: 10 },
  (_, index) => `/audio/blunder${index + 1}.m4a`,
);
