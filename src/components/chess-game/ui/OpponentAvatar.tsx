import {
  GHOST_AVATAR_SRC,
  getOpponentAvatarSrc,
  getOpponentResultAvatarSrc,
  type OpponentAvatarMood,
} from "../config";

type OpponentAvatarProps = {
  mode: "ghost" | "engine";
  engineElo: number;
  size?: number;
  className?: string;
  /** When set, shows the end-of-game victorious/defeated image (engine mode only). */
  mood?: OpponentAvatarMood | null;
};

export const OpponentAvatar = ({
  mode,
  engineElo,
  size = 28,
  className,
  mood,
}: OpponentAvatarProps) => {
  const src =
    mode === "ghost"
      ? GHOST_AVATAR_SRC
      : mood
        ? getOpponentResultAvatarSrc(engineElo, mood)
        : getOpponentAvatarSrc(engineElo);
  const classes = className
    ? `opponent-avatar ${className}`
    : "opponent-avatar";
  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      className={classes}
      aria-hidden="true"
    />
  );
};

export default OpponentAvatar;
