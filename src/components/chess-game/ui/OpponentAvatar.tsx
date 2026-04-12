import { GHOST_AVATAR_SRC, getOpponentAvatarSrc } from "../config";

type OpponentAvatarProps = {
  mode: "ghost" | "engine";
  engineElo: number;
  size?: number;
  className?: string;
};

export const OpponentAvatar = ({
  mode,
  engineElo,
  size = 28,
  className,
}: OpponentAvatarProps) => {
  const src = mode === "ghost" ? GHOST_AVATAR_SRC : getOpponentAvatarSrc(engineElo);
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
