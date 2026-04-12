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

export const GHOST_AVATAR_SRC = "/branding/ghost-logo-option-1-buddy.svg";

export const MAIA_OPPONENT_AVATARS: Record<
  (typeof MAIA_ELO_BINS)[number],
  string
> = {
  600: "/images/a.png",
  800: "/images/b.png",
  1000: "/images/gh600.png",
  1100: "/images/ghd.png",
  1200: "/images/gh800.png",
  1300: "/images/gh1000.png",
  1400: "/images/gha.png",
  1500: "/images/e.png",
  1600: "/images/ghf.png",
  1700: "/images/gh1100.png",
  1800: "/images/gh1200.png",
  1900: "/images/j.png",
  2000: "/images/gh1300.png",
  2200: "/images/l.png",
  2400: "/images/k.png",
  2600: "/images/gh1500.png",
};

const isMaiaBin = (elo: number): elo is (typeof MAIA_ELO_BINS)[number] =>
  (MAIA_ELO_BINS as readonly number[]).includes(elo);

export const getOpponentAvatarSrc = (engineElo: number): string => {
  if (isMaiaBin(engineElo)) {
    return MAIA_OPPONENT_AVATARS[engineElo];
  }
  let best: (typeof MAIA_ELO_BINS)[number] = MAIA_ELO_BINS[0];
  for (const bin of MAIA_ELO_BINS) {
    if (bin <= engineElo && bin > best) {
      best = bin;
    }
  }
  return MAIA_OPPONENT_AVATARS[best];
};

export const STARTING_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

export const ANALYSIS_UPLOAD_TIMEOUT_MS = 6000;

export const BLUNDER_AUDIO_CLIPS = Array.from(
  { length: 10 },
  (_, index) => `/audio/blunder${index + 1}.m4a`,
);
