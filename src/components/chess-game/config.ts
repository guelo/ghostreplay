/** Maia3 ELO bins – must match backend/app/maia3_client.py:ELO_BINS */
export const MAIA_ELO_BINS = [
  600, 800, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000,
  2200, 2400, 2600,
] as const;

/**
 1	Tiny Boo	harmless baby ghost
 2	Sleepy Wisp	gentle beginner
 3	Glowglide	friendly floating ghost
 4	Gigglegeist	silly but playful
 5	Timid Ticker	nervous little haunter
 6	Professor Pallor	smarter, trickier ghost
 7	Pawntergeist	game-themed ghost pun
 8	Frost Frown	first mildly hostile one
 9	Glower Wraith	confident and mean
 10	Droolshade	creepy but not deadly
 11	Scrapspirit	damaged/mechanical ghost
 12	Iron Haunt	stronger armored ghost
 13	Claw Wraith	dangerous melee ghost
 14	Crackskull Revenant	elite undead ghost
 15	Crimson Poltergeist	rage-powered menace
 16	Dreadnova	final-boss energy
 */
export const MAIA_BOT_NAMES: Record<(typeof MAIA_ELO_BINS)[number], string> = {
  600: "Blinky Boo 600",
  800: "Sleepy Wisp 800",
  1000: "Slobberboo 1000",
  1100: "Lollygeist 1100",
  1200: "Gigglegeist 1200",
  1300: "Glowglide 1300",
  1400: "Spellshade 1400",
  1500: "Chainling 1500",
  1600: "Murk Puff 1600",
  1700: "Sneerling 1700",
  1800: "Scowlshade 1800",
  1900: "Gloomclaw 1900",
  2000: "Dreadglare 2000",
  2200: "Crimson Poltergeist 2200",
  2400: "Gravechill 2400",
  2600: "The Hollow Maw 2600",
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
