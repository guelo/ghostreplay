import { useGameStore } from "../../../stores/useGameStore";
import SoundToggleIcon from "./SoundToggleIcon";

type SoundToggleButtonProps = {
  className: string;
};

const SoundToggleButton = ({ className }: SoundToggleButtonProps) => {
  const soundMuted = useGameStore((state) => state.soundMuted);
  const setSoundMuted = useGameStore((state) => state.setSoundMuted);

  return (
    <button
      className={className}
      type="button"
      aria-label={soundMuted ? "Unmute sound" : "Mute sound"}
      title={soundMuted ? "Unmute" : "Mute"}
      aria-pressed={soundMuted}
      onClick={() => setSoundMuted(!soundMuted)}
    >
      <SoundToggleIcon muted={soundMuted} />
    </button>
  );
};

export default SoundToggleButton;
